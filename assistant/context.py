import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from assistant.config import Project
from assistant.github_read import GitHubReader, GitHubUnavailable, RawChange, truncate_patch
from assistant.models import Change, Fact, KumaEvent, RunbookAnchor
from assistant.probe import ProbeResult
from assistant.rules import RuleResult
from assistant.runbook import parse_runbook

LOOKBACK = timedelta(hours=72)
EXCERPT_CHARS = 2000
# Commit messages can embed whole release notes (Dependabot), so they are capped too.
TITLE_CHARS = 200
BODY_CHARS = 500
# Shared by titles, bodies and diff excerpts of all changes.
DIFF_BUDGET = 12000
FACT_CHARS = 300
# Length of the marker truncate_patch appends; below this an excerpt is dropped entirely.
_MIN_EXCERPT = 20
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


@dataclass(frozen=True)
class IncidentContext:
    project: Project
    event: KumaEvent
    rule: RuleResult
    facts: tuple[Fact, ...]
    changes: tuple[Change, ...]
    anchors: tuple[RunbookAnchor, ...]
    github_ok: bool

    def ids(self) -> set[str]:
        return {item.id for group in (self.facts, self.changes, self.anchors) for item in group}


def _clean(text: str) -> str:
    # Fact texts are attacker-influenced: no control characters, bounded length.
    return _CONTROL.sub(" ", text)[:FACT_CHARS]


def _facts(event: KumaEvent, probe: ProbeResult | None, extra: list[str]) -> tuple[Fact, ...]:
    kuma = f"Kuma: {event.monitor_name} ist {event.status} – {event.msg}"
    rows = [("kuma", kuma)]
    if probe is not None:
        rows += [("probe", text) for text in probe.fact_texts()]
    rows += [("github", text) for text in extra]
    return tuple(Fact(id=f"F{n}", source=src, text=_clean(t)) for n, (src, t) in enumerate(rows, 1))


def _fit(text: str, max_chars: int, remaining: int) -> str:
    text = truncate_patch(text, max_chars)
    if len(text) > remaining:
        text = truncate_patch(text, remaining) if remaining >= _MIN_EXCERPT else ""
    return text


def _changes(raw: list[RawChange]) -> tuple[Change, ...]:
    ordered = sorted(raw, key=lambda r: r.merged_at, reverse=True)
    remaining = DIFF_BUDGET
    # Titles first: every change keeps its title in the issue list, even with the budget spent.
    titles = []
    for item in ordered:
        titles.append(_fit(item.title, TITLE_CHARS, remaining))
        remaining -= len(titles[-1])
    changes = []
    for number, (item, title) in enumerate(zip(ordered, titles, strict=True), 1):
        body = _fit(item.body, BODY_CHARS, remaining)
        remaining -= len(body)
        excerpt = _fit(item.patch, EXCERPT_CHARS, remaining)
        remaining -= len(excerpt)
        changes.append(
            Change(
                id=f"C{number}",
                repo=item.repo,
                sha=item.sha,
                title=title,
                author=item.author,
                merged_at=item.merged_at,
                files=item.files,
                diff_excerpt=excerpt,
                body=body,
            )
        )
    return tuple(changes)


def build_context(
    event: KumaEvent,
    project: Project,
    probe: ProbeResult | None,
    rule: RuleResult,
    reader: GitHubReader | None,
    now: datetime,
) -> IncidentContext:
    runs: list[str] = []
    raw: list[RawChange] = []
    anchors: list[RunbookAnchor] = []
    github_ok = True
    if reader is not None and project.repos:
        since = now - LOOKBACK
        try:
            for repo in project.repos:
                raw += reader.recent_changes(repo, since)
                runs += reader.deploy_runs(repo, since)
            for repo in project.runbook_repos:
                anchors += parse_runbook(repo, reader.readme(repo))
        except GitHubUnavailable:
            github_ok = False
            runs, raw, anchors = [], [], []
    notes = runs if github_ok else ["Änderungen nicht abrufbar (GitHub nicht erreichbar)"]
    return IncidentContext(
        project=project,
        event=event,
        rule=rule,
        facts=_facts(event, probe, notes),
        changes=_changes(raw),
        anchors=tuple(anchors),
        github_ok=github_ok,
    )
