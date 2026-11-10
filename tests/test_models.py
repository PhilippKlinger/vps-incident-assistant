import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from assistant.models import Analysis, parse_kuma

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_down():
    event = parse_kuma(load("kuma_down.json"))
    assert event is not None
    assert event.status == "down"
    assert event.monitor_name == "Videoflix · API"
    assert event.url == "https://videoflix.philipp-klinger.com/api/health/"
    assert event.msg == "Request failed with status code 502"
    assert event.time == datetime(2026, 11, 10, 14, 3, 12, 345000, tzinfo=UTC)


def test_parse_up():
    event = parse_kuma(load("kuma_up.json"))
    assert event is not None
    assert event.status == "up"
    assert event.monitor_name == "Videoflix · API"


def test_parse_test_message_returns_none():
    assert parse_kuma(load("kuma_test.json")) is None


def test_parse_garbage_returns_none():
    assert parse_kuma({}) is None
    assert parse_kuma({"heartbeat": {"status": 7}}) is None


def test_parse_pending_and_maintenance_return_none():
    for status in (2, 3):
        payload = load("kuma_down.json")
        payload["heartbeat"]["status"] = status
        assert parse_kuma(payload) is None


def test_parse_missing_name_or_bad_time_returns_none():
    no_name = load("kuma_down.json")
    del no_name["monitor"]["name"]
    assert parse_kuma(no_name) is None
    bad_time = load("kuma_down.json")
    bad_time["heartbeat"]["time"] = "yesterday"
    assert parse_kuma(bad_time) is None


def test_parse_iso_time_with_offset():
    payload = load("kuma_down.json")
    payload["heartbeat"]["time"] = "2026-11-10T15:03:12+01:00"
    event = parse_kuma(payload)
    assert event is not None
    assert event.time == datetime(2026, 11, 10, 14, 3, 12, tzinfo=UTC)


def test_parse_non_dict_payload_returns_none():
    assert parse_kuma(["not", "a", "dict"]) is None  # type: ignore[arg-type]
    assert parse_kuma({"heartbeat": "x", "monitor": "y"}) is None


def test_parse_unhashable_status_returns_none():
    payload = load("kuma_down.json")
    payload["heartbeat"]["status"] = [0]
    assert parse_kuma(payload) is None


def test_analysis_rejects_extra_field_and_empty_evidence():
    valid = {
        "suspected_change": "C1",
        "change_reasoning": "r",
        "runbook": "none",
        "runbook_reasoning": "r",
        "summary": "s",
        "evidence": ["F1"],
        "confidence": "low",
    }
    assert Analysis(**valid).evidence == ["F1"]
    with pytest.raises(ValidationError):
        Analysis(**valid, severity="high")
    with pytest.raises(ValidationError):
        Analysis(**{**valid, "evidence": []})
