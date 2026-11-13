import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from assistant.config import Config
from assistant.context import IncidentContext, build_context
from assistant.github_read import RawChange
from assistant.models import KumaEvent
from assistant.probe import ProbeResult
from assistant.rules import classify

DEPLOY_REPO = "PhilippKlinger/videoflix-deploy"
MIN_PREFIX = 7


def _key(repo: str) -> str:
    return repo.replace("/", "__")


class HistoryStore:
    """Frozen commit history and runbook sections, keyed by repo."""

    def __init__(self, commits: dict[str, list[dict]], runbooks: dict[str, str]):
        self._commits = commits
        self._runbooks = runbooks

    @classmethod
    def load(cls, root: Path) -> "HistoryStore":
        commits: dict[str, list[dict]] = {}
        for path in sorted((root / "history").glob("*.json")):
            entries = json.loads(path.read_text(encoding="utf-8"))
            commits[path.stem] = entries
        runbooks = {
            path.stem: path.read_text(encoding="utf-8")
            for path in sorted((root / "runbooks").glob("*.md"))
        }
        return cls(commits, runbooks)

    def commit(self, repo: str, sha_prefix: str) -> dict:
        found = [
            entry
            for entry in self._commits.get(_key(repo), [])
            if len(sha_prefix) >= MIN_PREFIX and entry["sha"].startswith(sha_prefix)
        ]
        if len(found) != 1:
            raise KeyError(f"{repo}@{sha_prefix}: {len(found)} matching commits")
        return found[0]

    def runbook(self, repo: str) -> str:
        try:
            return self._runbooks[_key(repo)]
        except KeyError:
            raise KeyError(f"no frozen runbook for {repo}") from None


@dataclass(frozen=True)
class Expected:
    category: str
    suspected_change: str  # full SHA or "none"
    runbook: str  # anchor id or "none"


@dataclass(frozen=True)
class Case:
    id: str
    group: str
    note: str
    context: IncidentContext
    expected: Expected
    canary: str | None
    change_shas: dict[str, str]  # change id (C1, ...) -> full SHA


class _FixtureReader:
    """Serves the frozen data through the calls build_context makes on the GitHub reader."""

    def __init__(self, changes: list[RawChange], runs: list[str], store: HistoryStore):
        self._changes = changes
        self._runs = runs
        self._store = store

    def recent_changes(self, repo: str, since: datetime) -> list[RawChange]:
        return [c for c in self._changes if c.repo == repo and c.merged_at >= since]

    def deploy_runs(self, repo: str, since: datetime) -> list[str]:
        return self._runs if repo == DEPLOY_REPO else []

    def runbook(self, repo: str) -> str:
        return self._store.runbook(repo)


def _raw_change(entry: dict, merged_at: datetime) -> RawChange:
    return RawChange(
        repo=entry["repo"],
        sha=entry["sha"],
        title=entry["title"],
        author=entry["author"],
        merged_at=merged_at,
        files=tuple(entry["files"]),
        patch=entry["patch"],
        body=entry["body"],
    )


def load_case(path: Path, store: HistoryStore, config: Config) -> Case:
    raw = json.loads(path.read_text(encoding="utf-8"))
    event = KumaEvent(**raw["event"])
    probe = None
    if raw["probe"] is not None:
        probe = ProbeResult(
            **{**raw["probe"], "addresses": tuple(raw["probe"]["addresses"])},
        )

    changes = [
        _raw_change(
            store.commit(item["repo"], item["sha"]),
            event.time - timedelta(minutes=item["minutes_before"]),
        )
        for item in raw["history"]
    ]
    if raw["cause"] is not None:
        cause = raw["cause"]
        changes.append(_raw_change(cause, event.time - timedelta(minutes=cause["minutes_before"])))

    reader = _FixtureReader(changes, raw["deploy_runs"], store)
    project = config.project_for(event.monitor_name)
    context = build_context(event, project, probe, classify(event, probe), reader, now=event.time)
    return Case(
        id=raw["id"],
        group=raw["group"],
        note=raw["note"],
        context=context,
        expected=Expected(**raw["expected"]),
        canary=raw["canary"],
        change_shas={c.id: c.sha for c in context.changes},
    )


def load_cases(directory: Path, store: HistoryStore, config: Config) -> list[Case]:
    cases = [load_case(path, store, config) for path in directory.glob("*.json")]
    return sorted(cases, key=lambda case: case.id)
