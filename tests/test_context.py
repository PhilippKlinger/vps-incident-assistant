from datetime import UTC, datetime, timedelta

import pytest

from assistant.config import Project
from assistant.context import build_context
from assistant.github_read import GitHubUnavailable, RawChange
from assistant.models import KumaEvent
from assistant.probe import ProbeResult
from assistant.rules import Category, RuleResult, Severity

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PROJECT = Project("videoflix", "Videoflix", ("Videoflix · API",), ("o/a", "o/b"), ("o/a",), True)
RULE = RuleResult(Category.upstream_502, Severity.high, "The URL returns HTTP 502.")
EVENT = KumaEvent(
    monitor_name="Videoflix · API",
    url="https://v.example/",
    status="down",
    msg="bad\ngateway\x00" + "x" * 400,
    time=NOW,
)
PROBE = ProbeResult(502, None, 30, {"server": "caddy"}, 60, ("1.2.3.4",))


def _raw(repo, i, patch="p", minutes=0, title=None, body=""):
    when = NOW - timedelta(minutes=minutes)
    return RawChange(repo, f"sha{repo}{i}", title or f"t{i}", "alice", when, ("f",), patch, body)


class FakeReader:
    def __init__(self, changes=None, runs=None, fail=False):
        self.changes = changes or {}
        self.runs = runs or {}
        self.fail = fail
        self.calls = []

    def recent_changes(self, repo, since):
        self.calls.append(("changes", repo, since))
        if self.fail:
            raise GitHubUnavailable("down")
        return self.changes.get(repo, [])

    def deploy_runs(self, repo, since):
        if self.fail:
            raise GitHubUnavailable("down")
        return self.runs.get(repo, [])

    def runbook(self, repo):
        if self.fail:
            raise GitHubUnavailable("down")
        return "## Troubleshooting\n\n### Proxy down\n\nText\n"


def test_context_ids_are_sequential():
    reader = FakeReader(
        changes={"o/a": [_raw("o/a", 1, minutes=30)], "o/b": [_raw("o/b", 2, minutes=5)]},
        runs={"o/a": ["Deploy 1"]},
    )

    ctx = build_context(EVENT, PROJECT, PROBE, RULE, reader, NOW)

    fact_ids = [f.id for f in ctx.facts]
    assert fact_ids == [f"F{i}" for i in range(1, len(fact_ids) + 1)]
    assert ctx.facts[0].source == "kuma"
    assert ctx.facts[0].text.startswith("Kuma: Videoflix · API is down – bad gateway")
    assert all(
        len(f.text) <= 300 and "\n" not in f.text and "\x00" not in f.text for f in ctx.facts
    )
    assert ctx.facts[1].source == "probe"
    assert ctx.facts[-1].source == "github" and ctx.facts[-1].text == "Deploy 1"
    assert [c.id for c in ctx.changes] == ["C1", "C2"]
    assert [c.repo for c in ctx.changes] == ["o/b", "o/a"]
    assert [a.id for a in ctx.anchors] == ["a#proxy-down"]
    assert ctx.ids() == set(fact_ids) | {"C1", "C2", "a#proxy-down"}
    assert ctx.github_ok is True
    assert reader.calls[0][2] == NOW - timedelta(hours=72)


def test_context_diff_budget():
    big = "x" * 2000
    reader = FakeReader(
        changes={
            "o/a": [_raw("o/a", i, big, minutes=i) for i in range(10)],
            "o/b": [_raw("o/b", i, big, minutes=i) for i in range(5)],
        }
    )

    ctx = build_context(EVENT, PROJECT, PROBE, RULE, reader, NOW)

    assert len(ctx.changes) == 15
    assert sum(len(c.diff_excerpt) for c in ctx.changes) <= 12000
    assert ctx.changes[0].diff_excerpt != ""


def test_context_caps_commit_title_and_body():
    raw = _raw("o/a", 1, title="T" * 1000, body="release notes\n" * 1000)
    reader = FakeReader(changes={"o/a": [raw]})

    change = build_context(EVENT, PROJECT, PROBE, RULE, reader, NOW).changes[0]

    assert len(change.title) <= 200 and change.title.startswith("TTT")
    assert len(change.body) <= 500 and change.body.endswith("\n… truncated")


def test_context_budget_counts_title_and_body():
    big = "x" * 2000
    changes = [
        _raw("o/a", i, big, minutes=i, title=f"t{i} " + "T" * 300, body="b\n" * 1000)
        for i in range(10)
    ]
    reader = FakeReader(changes={"o/a": changes})

    ctx = build_context(EVENT, PROJECT, PROBE, RULE, reader, NOW)

    total = sum(len(c.title) + len(c.body) + len(c.diff_excerpt) for c in ctx.changes)
    assert total <= 12000
    # Titles are counted first, so every change keeps its title in the issue list.
    assert all(c.title.startswith(f"t{i} ") for i, c in enumerate(ctx.changes))
    assert ctx.changes[0].body != "" and ctx.changes[-1].diff_excerpt == ""


def test_context_without_github():
    ctx = build_context(EVENT, PROJECT, PROBE, RULE, FakeReader(fail=True), NOW)

    assert ctx.github_ok is False
    assert ctx.changes == () and ctx.anchors == ()
    assert "unavailable" in ctx.facts[-1].text
    assert ctx.facts[0].source == "kuma" and ctx.facts[1].source == "probe"
    assert all("Deploy" not in f.text for f in ctx.facts)


def test_context_without_reader_or_probe():
    ctx = build_context(EVENT, PROJECT, None, RULE, None, NOW)

    assert ctx.github_ok is True
    assert ctx.changes == () and ctx.anchors == ()
    assert [f.source for f in ctx.facts] == ["kuma"]


def test_context_is_frozen():
    ctx = build_context(EVENT, PROJECT, PROBE, RULE, None, NOW)
    with pytest.raises(AttributeError):
        ctx.github_ok = False
