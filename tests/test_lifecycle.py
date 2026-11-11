from datetime import UTC, datetime, timedelta

from assistant.lifecycle import (
    INCIDENT,
    RESOLVED,
    Action,
    Issue,
    decide,
    monitor_label,
    project_label,
)
from assistant.models import KumaEvent

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=10)
API = "monitor:videoflix-api"
WEB = "monitor:videoflix-web"


def _event(status):
    return KumaEvent(monitor_name="Videoflix · API", url=None, status=status, msg="", time=NOW)


def _issue(*labels, age=timedelta(minutes=1)):
    return Issue(
        number=7,
        labels=frozenset((INCIDENT, project_label("videoflix"), *labels)),
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - age,
    )


def _decide(status, issue):
    return decide(_event(status), issue, API, NOW, WINDOW)


def test_down_without_issue_creates():
    assert _decide("down", None) is Action.CREATE


def test_duplicate_down_is_ignored():
    assert _decide("down", _issue(API)) is Action.IGNORE


def test_duplicate_down_at_window_edge_is_ignored():
    assert _decide("down", _issue(API, age=WINDOW)) is Action.IGNORE


def test_same_monitor_down_after_window_appends():
    # A lost "up" must not silence the monitor for good.
    issue = _issue(API, age=WINDOW + timedelta(seconds=1))
    assert _decide("down", issue) is Action.APPEND


def test_down_for_new_monitor_appends():
    assert _decide("down", _issue(WEB)) is Action.APPEND


def test_down_on_resolved_issue_within_window_reopens():
    assert _decide("down", _issue(RESOLVED, age=WINDOW)) is Action.REOPEN_FLAP


def test_down_on_resolved_issue_after_window_creates():
    issue = _issue(RESOLVED, age=WINDOW + timedelta(seconds=1))
    assert _decide("down", issue) is Action.CREATE


def test_up_without_issue_is_ignored():
    assert _decide("up", None) is Action.IGNORE


def test_up_without_monitor_label_is_ignored():
    assert _decide("up", _issue(WEB)) is Action.IGNORE


def test_up_on_resolved_issue_is_ignored():
    assert _decide("up", _issue(RESOLVED)) is Action.IGNORE


def test_up_with_other_monitors_down_marks_monitor_up():
    assert _decide("up", _issue(API, WEB)) is Action.MONITOR_UP


def test_up_for_last_monitor_resolves():
    assert _decide("up", _issue(API)) is Action.RESOLVE


def test_monitor_label_slug():
    assert monitor_label("Videoflix · API") == "monitor:videoflix-api"
    assert monitor_label("Neuro Check-in · Web") == "monitor:neuro-check-in-web"
    assert monitor_label("Statusseite") == "monitor:statusseite"


def test_monitor_label_is_capped():
    assert len(monitor_label("x" * 200)) == 50


def test_project_label():
    assert project_label("videoflix") == "project:videoflix"
