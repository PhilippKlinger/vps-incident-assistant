from dataclasses import replace
from datetime import UTC, datetime

from assistant.models import Change, Fact, RunbookAnchor
from evals.baselines import Answer, keyword_match, latest_change
from tests.evals_support import load_fixture_set


def _change(number: int, files: tuple[str, ...]) -> Change:
    return Change(
        id=f"C{number}",
        repo="PhilippKlinger/vps-edge",
        sha=f"{number:040x}",
        title="t",
        author="a",
        merged_at=datetime(2026, 11, 5, tzinfo=UTC),
        files=files,
        diff_excerpt="",
        body="",
    )


def _case(msg: str, changes: tuple[Change, ...], titles: tuple[str, ...] = ()):
    _, cases = load_fixture_set()
    case = cases[1]  # no probe, category unknown
    event = case.context.event.model_copy(update={"msg": msg})
    facts = (Fact(id="F1", source="kuma", text=f"Kuma: x is down – {msg}"),)
    anchors = tuple(
        RunbookAnchor(id=f"r#{i}", repo="r", title=t, text="") for i, t in enumerate(titles)
    )
    context = replace(case.context, event=event, facts=facts, changes=changes, anchors=anchors)
    return replace(case, context=context)


def test_latest_change_is_c1():
    _, cases = load_fixture_set()

    assert latest_change(cases[0]) == Answer("C1", "none")


def test_latest_change_without_changes():
    case = _case("x", ())

    assert latest_change(case) == Answer("none", "none")


def test_keyword_prefers_the_change_touching_symptom_files():
    proxy = _change(2, ("Caddyfile", "proxy/502.conf"))
    docs = _change(1, ("README.md",))

    case = _case("502 Bad Gateway", (docs, proxy))

    assert keyword_match(case).suspected_change == "C2"


def test_keyword_tie_goes_to_the_newer_change():
    newer = _change(1, ("proxy/a.conf",))
    older = _change(2, ("proxy/b.conf",))

    case = _case("proxy broke", (newer, older))

    assert keyword_match(case).suspected_change == "C1"


def test_keyword_no_overlap_is_none():
    case = _case("502 Bad Gateway", (_change(1, ("README.md",)),), ("Certificate renewal",))

    assert keyword_match(case) == Answer("none", "none")


def test_keyword_runbook_by_title_overlap_first_wins_ties():
    case = _case("502 Bad Gateway", (), ("Disk full", "502 from the proxy", "502 again"))

    assert keyword_match(case).runbook == "r#1"


def test_keyword_ignores_stopwords_and_short_tokens():
    # "status code" and "the" are stopwords, "ok" is too short.
    case = _case("the status code ok", (_change(1, ("status/code/the/ok.txt",)),))

    assert keyword_match(case).suspected_change == "none"


def test_keyword_uses_rule_category_and_probe_facts():
    _, cases = load_fixture_set()
    case = cases[0]  # category upstream_502, probe fact "Header server: Caddy"
    changes = (_change(1, ("README.md",)), _change(2, ("caddy/upstream.conf",)))

    answer = keyword_match(replace(case, context=replace(case.context, changes=changes)))

    assert answer.suspected_change == "C2"
