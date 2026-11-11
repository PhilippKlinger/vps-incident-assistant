import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from assistant.llm import OllamaClient
from evals.baselines import Answer
from evals.meter import NullMeter
from evals.runner import candidate_summary, run_candidate, run_case
from tests.evals_support import FIXTURES, load_fixture_set


def _analysis(change="C1", runbook="vps-edge#upstream-502", summary="The proxy drops requests."):
    return {
        "suspected_change": change,
        "change_reasoning": "It touched the config.",
        "runbook": runbook,
        "runbook_reasoning": "Matches the 502 section.",
        "summary": summary,
        "evidence": ["C1"],
        "confidence": "medium",
    }


def _ollama(handler) -> OllamaClient:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaClient(client, "http://ollama.test", "m")


def _reply(content) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"message": {"content": text}})


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 2.0
        return self.now


class FakeMeter:
    def start(self):
        pass

    def stop(self):
        return 4.5, 1234.5


def test_valid_answer_is_recorded():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis()))

    run = run_case(cases[0], ollama, FakeMeter(), Clock())

    assert run.case_id == "app-01-cors"
    assert run.answer == Answer("C1", "vps-edge#upstream-502")
    assert run.schema_valid_first is True
    assert run.unavailable is None
    assert (run.cpu_s, run.ram_peak_mib) == (4.5, 1234.5)
    assert run.latency_s == 2.0
    assert run.unsupported == ()


def test_invalid_then_valid_answer_is_not_valid_first_try():
    _, cases = load_fixture_set()
    replies = iter(["not json", _analysis()])
    ollama = _ollama(lambda request: _reply(next(replies)))

    run = run_case(cases[0], ollama, NullMeter(), Clock())

    assert run.schema_valid_first is False
    assert run.answer == Answer("C1", "vps-edge#upstream-502")


def test_unknown_change_id_counts_as_invalid():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis(change="C42")))

    run = run_case(cases[0], ollama, NullMeter(), Clock())

    assert (run.answer, run.unavailable) == (None, "invalid response")


def test_unreachable_model_is_recorded_and_the_run_continues():
    _, cases = load_fixture_set()
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= 2:
            raise httpx.ConnectError("refused")
        return _reply(_analysis(change="none", runbook="none"))

    ollama = _ollama(handler)

    result = run_candidate(cases, ollama, NullMeter(), "m")

    first, second = result["runs"]
    assert (first["answer"], first["unavailable"]) == (None, "model unreachable")
    assert first["latency_s"] >= 0
    assert second["answer"] == {"suspected_change": "none", "runbook": "none"}
    assert len(calls) == 3


def test_timeout_is_reported_as_timed_out():
    _, cases = load_fixture_set()

    def handler(request):
        raise httpx.ReadTimeout("slow")

    run = run_case(cases[0], _ollama(handler), NullMeter(), Clock())

    assert run.unavailable == "model timed out"
    assert run.schema_valid_first is False
    assert run.latency_s == 2.0  # the clock is read once before and once after both attempts


def test_canary_in_the_published_comment_is_unsupported():
    _, cases = load_fixture_set()
    case = replace(cases[0], canary="CANARY-7731")
    ollama = _ollama(lambda request: _reply(_analysis(summary="Leaked CANARY-7731 here.")))

    run = run_case(case, ollama, NullMeter(), Clock())

    assert run.unsupported == ("canary",)


def test_the_comment_own_runbook_link_is_not_a_violation():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis()))

    run = run_case(cases[0], ollama, NullMeter(), Clock())

    assert run.unsupported == ()


def test_candidate_summary():
    _, cases = load_fixture_set()
    ollama = _ollama(
        lambda request: _reply(_analysis(change="C1", runbook="vps-edge#upstream-502"))
    )

    result = run_candidate(cases, ollama, FakeMeter(), "m")

    assert result["model"] == "m"
    assert len(result["runs"]) == 2
    assert result["metrics"]["change_hit"] == 100.0
    assert result["metrics"]["none_change"] == 0.0
    assert result["schema_valid_first_pct"] == 100.0
    assert result["unsupported_count"] == 0
    assert result["latency_p95_s"] >= 0
    assert result["cpu_s_mean"] == 4.5
    assert result["ram_peak_mib_max"] == 1234.5


def _main(*argv):
    from evals.__main__ import main

    return main([*argv, "--cases", str(FIXTURES / "cases"), "--data", str(FIXTURES)])


def test_cli_run_writes_a_candidate_file(tmp_path, monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _reply(_analysis())))
    monkeypatch.setattr("evals.__main__._client", lambda: client)
    out = tmp_path / "run.json"

    code = _main("run", "--model", "m", "--only", "app-01-cors", "--out", str(out))

    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0
    assert data["model"] == "m"
    assert [run["case_id"] for run in data["runs"]] == ["app-01-cors"]


def test_cli_run_rejects_an_unknown_case_id(tmp_path):
    try:
        _main("run", "--model", "m", "--only", "nope", "--out", str(tmp_path / "x.json"))
    except SystemExit as exit_:
        assert "Unknown case ids: nope" in str(exit_)
    else:
        raise AssertionError("expected SystemExit")


def test_cli_report_writes_json_and_markdown(tmp_path, monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _reply(_analysis())))
    monkeypatch.setattr("evals.__main__._client", lambda: client)
    run_file = tmp_path / "run.json"
    _main("run", "--model", "m", "--out", str(run_file))

    code = _main("report", "--runs", str(run_file), "--out-dir", str(tmp_path / "out"))

    assert code == 0
    assert (tmp_path / "out" / "report.json").is_file()
    assert "# Incident assistant eval report" in (tmp_path / "out" / "report.md").read_text(
        encoding="utf-8"
    )


def test_cli_candidates_prints_matrix_and_existing_smoke_cases(capsys):
    assert _main("candidates") == 0

    data = json.loads(capsys.readouterr().out)
    assert [c["model"] for c in data["matrix"]] == [
        "qwen3.5:9b-q4_K_M",
        "qwen2.5-coder:7b-instruct-q4_K_M",
        "qwen3.5:4b-q4_K_M",
    ]
    assert data["smoke_model"] == "qwen3.5:4b-q4_K_M"
    # The smoke run fails in CI if an id is missing, so pin them to the real case set.
    cases_dir = Path(__file__).parents[1] / "evals" / "cases"
    case_ids = {path.stem for path in cases_dir.glob("*.json")}
    assert len(data["smoke_cases"]) == 3
    assert set(data["smoke_cases"]) <= case_ids


def test_underscore_canary_is_found_after_sanitizing():
    _, cases = load_fixture_set()
    case = replace(cases[0], canary="CANARY_7731")
    ollama = _ollama(lambda request: _reply(_analysis(summary="Leaked CANARY_7731 here.")))

    run = run_case(case, ollama, NullMeter(), Clock())

    assert run.unsupported == ("canary",)


def test_meter_is_stopped_even_when_the_attempt_loop_raises():
    _, cases = load_fixture_set()
    stopped = []

    class Meter:
        def start(self):
            pass

        def stop(self):
            stopped.append(True)
            return None, None

    def handler(request):
        raise RuntimeError("bug")

    try:
        run_case(cases[0], _ollama(handler), Meter(), Clock())
    except RuntimeError:
        pass

    assert stopped == [True]


def test_missing_cases_count_as_unavailable():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis()))
    full = run_candidate(cases, ollama, NullMeter(), "m")

    summary = candidate_summary(cases, full["runs"][:1])

    # app-01-cors answered correctly; none-01-quiet is missing, so wrong.
    assert summary["metrics"]["change_hit"] == 100.0
    assert summary["metrics"]["none_change"] == 0.0
    assert summary["schema_valid_first_pct"] == 50.0


def test_run_with_unknown_case_ids_is_rejected():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis()))
    runs = run_candidate(cases, ollama, NullMeter(), "m")["runs"]
    runs[0] = {**runs[0], "case_id": "ghost"}

    with pytest.raises(SystemExit, match="Unknown case ids in run: ghost"):
        candidate_summary(cases, runs)
