from datetime import UTC, datetime, timedelta

from assistant.config import Project
from assistant.context import IncidentContext
from assistant.models import Analysis, Change, Fact, KumaEvent, RunbookAnchor
from assistant.render import (
    analysis_comment,
    append_comment,
    flap_comment,
    issue_body,
    issue_title,
    monitor_up_comment,
    resolved_comment,
    sanitize,
    telegram_text,
    unavailable_comment,
)
from assistant.rules import Category, RuleResult, Severity

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PROJECT = Project(
    "videoflix", "Videoflix", ("Videoflix · API",), ("o/vps-edge",), ("o/vps-edge",), True
)
RULE = RuleResult(Category.upstream_502, Severity.high, "Die Adresse antwortet mit HTTP 502.")
EVENT = KumaEvent(monitor_name="Videoflix · API", url=None, status="down", msg="m", time=NOW)
CHANGE = Change(
    id="C3",
    repo="PhilippKlinger/vps-edge",
    sha="a1b2c3d4e5f6",
    title="fix: tune *proxy* @bob",
    author="alice",
    merged_at=NOW,
    files=("Caddyfile",),
    diff_excerpt="",
    body="",
)
ANCHOR = RunbookAnchor(
    id="vps-edge#upstream-502",
    repo="PhilippKlinger/vps-edge",
    title="Upstream 502",
    text="t",
)
FACTS = (
    Fact(id="F1", source="kuma", text="Kuma: down"),
    Fact(id="F2", source="probe", text="HTTP 502"),
)
ISSUE_URL = "https://github.com/o/r/issues/12"


def make_ctx(changes=(CHANGE,), github_ok=True, event=EVENT):
    return IncidentContext(PROJECT, event, RULE, FACTS, tuple(changes), (ANCHOR,), github_ok)


def make_analysis(**over):
    data = {
        "suspected_change": "C3",
        "change_reasoning": "Passt zeitlich.",
        "runbook": "vps-edge#upstream-502",
        "runbook_reasoning": "Gleiches Symptom.",
        "summary": "Proxy erreicht die App nicht.",
        "evidence": ["F1", "C3"],
        "confidence": "medium",
    }
    return Analysis(**{**data, **over})


def test_sanitize_removes_links():
    out = sanitize("see https://evil.example/x and HTTP://a.b and www.evil.example now", 300)
    assert "evil" not in out and "http" not in out.lower() and "www" not in out


def test_sanitize_neutralizes_mentions():
    out = sanitize("ping @PhilippKlinger", 300)
    assert "@" not in out
    assert "(at)PhilippKlinger" in out


def test_sanitize_strips_html_and_images():
    out = sanitize("<img src=x> ![x](y)", 300)
    assert "<" not in out and ">" not in out
    assert out == "img src=x \\!\\[x\\]\\(y\\)"


def test_sanitize_collapses_whitespace_and_escapes_backslash():
    assert sanitize("a\n\n b\t`c` \\", 300) == "a b \\`c\\` \\\\"


def test_sanitize_truncates():
    assert len(sanitize("x" * 500, 300)) == 300


def test_sanitize_never_ends_in_dangling_backslash():
    # "ab\*cd" cut at 3 would end in a lone backslash
    assert sanitize("ab*cd", 3) == "ab"
    assert sanitize("ab*cd", 4) == "ab\\*"
    out = sanitize("\\" * 10, 5)
    assert len(out) <= 5 and out == "\\\\\\\\"


def test_issue_title():
    assert issue_title(make_ctx()) == "Störung: Videoflix – upstream_502 (Videoflix · API)"


def test_issue_title_sanitizes_monitor_name():
    event = EVENT.model_copy(update={"monitor_name": "A @x <b>"})
    assert "@" not in issue_title(make_ctx(event=event))


def test_issue_body_sections():
    body = issue_body(make_ctx(), analysis_pending=True)
    assert "### Regel-Ergebnis" in body
    assert "upstream_502" in body and "high" in body and RULE.reason in body
    assert "### Fakten" in body and "- F1: Kuma: down" in body
    assert "### Änderungen im Zeitfenster" in body
    assert "- C3 · vps-edge · a1b2c3d · fix: tune \\*proxy\\* (at)bob" in body
    assert "Analyse folgt" in body and "ohne LLM-Analyse" not in body


def test_issue_body_without_llm():
    body = issue_body(make_ctx(), analysis_pending=False)
    assert "ohne LLM-Analyse" in body and "Analyse folgt" not in body


def test_issue_body_changes_none_and_unavailable():
    assert "keine" in issue_body(make_ctx(changes=()), analysis_pending=False)
    body = issue_body(make_ctx(changes=(), github_ok=False), analysis_pending=False)
    assert "nicht abrufbar" in body


def test_append_and_flap_comments():
    append = append_comment(make_ctx())
    assert "Weiterer Alarm" in append and "Videoflix · API" in append
    assert "upstream_502" in append and "- F2: HTTP 502" in append
    flap = flap_comment(make_ctx())
    assert "Erneut gestört kurz nach der Erholung" in flap
    assert "upstream_502" in flap and "- F1: Kuma: down" in flap


def test_resolved_comment_duration():
    assert resolved_comment(timedelta(minutes=12, seconds=59)) == (
        "Erholt nach 12 min. Nachbetrachtung offen."
    )
    assert resolved_comment(timedelta(seconds=5)).startswith("Erholt nach 1 min.")


def test_unavailable_comment():
    assert unavailable_comment("Zeitüberschreitung") == (
        "Analyse nicht verfügbar: Zeitüberschreitung. Es gilt das Regel-Ergebnis oben."
    )


def test_analysis_comment_says_vermutung():
    out = analysis_comment(make_analysis(), make_ctx())
    assert out.startswith("### Analyse (Vermutung)")
    assert "C3 · vps-edge · a1b2c3d" in out
    assert "F1, C3" in out
    assert "mittel" in out
    assert "Passt zeitlich." in out and "Gleiches Symptom." in out


def test_analysis_comment_link_only_from_anchor():
    analysis = make_analysis(
        summary="siehe https://evil.example/x",
        change_reasoning="auch www.evil.example",
        runbook_reasoning="und HTTP://evil.example",
    )
    out = analysis_comment(analysis, make_ctx())
    assert out.lower().count("http") == 1
    assert "[Upstream 502](https://github.com/PhilippKlinger/vps-edge#upstream-502)" in out
    assert "evil" not in out


def test_analysis_comment_none_and_unknown_ids():
    out = analysis_comment(make_analysis(suspected_change="none", runbook="none"), make_ctx())
    assert "keine bestimmte Änderung" in out and "kein passender Abschnitt" in out
    assert "http" not in out
    out = analysis_comment(make_analysis(suspected_change="C9", runbook="x#y"), make_ctx())
    assert out.count("unbekannt") == 2


def test_telegram_text_with_analysis():
    text = telegram_text(make_ctx(), ISSUE_URL, make_analysis())
    assert text == (
        "Videoflix: vermutete Ursache vps-edge a1b2c3d · Runbook: Upstream 502 · Issue #12"
        f" · {ISSUE_URL}"
    )


def test_telegram_text_with_analysis_none_parts():
    analysis = make_analysis(suspected_change="none", runbook="none")
    text = telegram_text(make_ctx(), ISSUE_URL, analysis)
    assert text.startswith("Videoflix: vermutete Ursache keine · Runbook: keine · Issue #12")


def test_telegram_text_without_analysis():
    text = telegram_text(make_ctx(), ISSUE_URL, None)
    assert text == f"Videoflix: upstream_502 (high) · {RULE.reason} · {ISSUE_URL}"


def test_sanitize_does_not_reassemble_links_from_brackets():
    for text in ("h<ttps://evil.example/x", "w<ww.evil.example", "http>s://evil.example/x"):
        out = sanitize(text, 300)
        assert "://" not in out and "www." not in out.lower()


def test_sanitize_removes_any_scheme():
    for text in ("ftp://evil.example/x", "FTP://evil.example/x", "ssh+git://evil.example/x"):
        out = sanitize(text, 300)
        assert "://" not in out and "evil" not in out


def test_sanitize_escapes_ampersand_entities():
    for text in ("&commat;PhilippKlinger", "&#64;x"):
        out = sanitize(text, 300)
        assert "@" not in out
        assert all(out[i - 1] == "\\" for i, ch in enumerate(out) if ch == "&")


def test_sanitize_breaks_issue_references():
    out = sanitize("PhilippKlinger/videoflix-backend#1 and #12", 300)
    assert "\\#1" not in out and "\\# 1" in out and "\\# 12" in out


def test_telegram_text_unknown_ids():
    analysis = make_analysis(suspected_change="C9", runbook="x#y")
    text = telegram_text(make_ctx(), ISSUE_URL, analysis)
    assert "vermutete Ursache unbekannt · Runbook: unbekannt" in text


def test_monitor_up_comment_sanitizes_name():
    text = monitor_up_comment("Videoflix · API @evil")
    assert "wieder erreichbar" in text
    assert "@" not in text
