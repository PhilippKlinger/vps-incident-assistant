import json
import shutil

import pytest

from assistant.config import load_config
from assistant.models import KumaEvent
from assistant.probe import ProbeResult
from assistant.rules import classify
from evals.cases import HistoryStore, load_case, load_cases
from tests.evals_support import CONFIG, FIXTURES, load_fixture_set


def test_load_cases_sorted_by_id():
    _, cases = load_fixture_set()

    assert [c.id for c in cases] == ["app-01-cors", "none-01-quiet"]


def test_newest_change_is_c1_with_full_sha():
    _, cases = load_fixture_set()
    case = cases[0]

    assert case.context.changes[0].title == "feat: drop cors header"
    assert case.change_shas["C1"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert case.change_shas["C2"].startswith("814a76b2")
    assert len(case.context.changes) == 3


def test_anchors_come_from_the_frozen_runbooks():
    _, cases = load_fixture_set()

    ids = [a.id for a in cases[0].context.anchors]
    assert "vps-edge#upstream-502" in ids
    assert "videoflix-deploy#deploy-failed" in ids


def test_rule_comes_from_classify_on_the_same_input():
    _, cases = load_fixture_set()
    raw = json.loads((FIXTURES / "cases" / "app-01-cors.json").read_text(encoding="utf-8"))
    event = KumaEvent(**raw["event"])
    probe = ProbeResult(
        status_code=502,
        error=None,
        latency_ms=41,
        headers={"server": "Caddy"},
        tls_days_left=60,
        addresses=("1.2.3.4",),
    )

    assert cases[0].context.rule == classify(event, probe)


def test_deploy_runs_and_probe_facts_reach_the_context():
    _, cases = load_fixture_set()

    texts = [f.text for f in cases[0].context.facts]
    assert "HTTP 502 in 41 ms" in texts
    assert any(t.startswith("Deploy 2026-11-05") for t in texts)


def test_case_without_probe_or_cause():
    _, cases = load_fixture_set()
    case = cases[1]

    assert case.expected.suspected_change == "none"
    assert len(case.context.changes) == 2
    assert case.canary is None


def test_unknown_history_sha_raises(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "data")
    path = tmp_path / "data" / "cases" / "none-01-quiet.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["history"][0]["sha"] = "deadbeef"
    path.write_text(json.dumps(raw), encoding="utf-8")
    store = HistoryStore.load(tmp_path / "data")

    with pytest.raises(KeyError):
        load_case(path, store, load_config(CONFIG))


def test_ambiguous_prefix_raises():
    store = HistoryStore.load(FIXTURES)

    with pytest.raises(KeyError):
        store.commit("PhilippKlinger/vps-edge", "814a76b")


def test_missing_directory_gives_no_cases(tmp_path):
    store = HistoryStore.load(FIXTURES)

    assert load_cases(tmp_path / "nope", store, load_config(CONFIG)) == []
