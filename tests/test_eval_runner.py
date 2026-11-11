import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from assistant.llm import OllamaClient
from evals.baselines import Answer
from evals.meter import NullMeter
from evals.runner import candidate_summary, fetch_provenance, run_candidate, run_case
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
    assert run.first_attempt == "valid"
    assert run.unavailable is None
    assert (run.cpu_s, run.ram_peak_mib) == (4.5, 1234.5)
    assert run.latency_s == 2.0
    assert run.unsupported == ()


def test_invalid_then_valid_answer_is_not_valid_first_try():
    _, cases = load_fixture_set()
    replies = iter(["not json", _analysis()])
    ollama = _ollama(lambda request: _reply(next(replies)))

    run = run_case(cases[0], ollama, NullMeter(), Clock())

    assert run.first_attempt == "schema"
    assert run.answer == Answer("C1", "vps-edge#upstream-502")


def test_unknown_change_id_counts_as_invalid():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis(change="C42")))

    run = run_case(cases[0], ollama, NullMeter(), Clock())

    assert (run.answer, run.unavailable) == (None, "invalid response")
    assert (run.first_attempt, run.rejection) == ("unknown_change", "unknown_change")


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
    assert first["first_attempt"] == "unreachable"
    assert first["latency_s"] >= 0
    assert second["answer"] == {"suspected_change": "none", "runbook": "none"}
    assert len(calls) == 3


def test_timeout_is_reported_as_timed_out():
    _, cases = load_fixture_set()

    def handler(request):
        raise httpx.ReadTimeout("slow")

    run = run_case(cases[0], _ollama(handler), NullMeter(), Clock())

    assert run.unavailable == "model timed out"
    assert run.first_attempt == "timed out"
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
    assert (result["first_attempt_timeouts"], result["first_attempt_unreachable"]) == (0, 0)
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
    # Only cases that were run count for the schema rate.
    assert summary["schema_valid_first_pct"] == 100.0


def test_run_with_unknown_case_ids_is_rejected():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis()))
    runs = run_candidate(cases, ollama, NullMeter(), "m")["runs"]
    runs[0] = {**runs[0], "case_id": "ghost"}

    with pytest.raises(SystemExit, match="Unknown case ids in run: ghost"):
        candidate_summary(cases, runs)


def test_canary_is_found_case_insensitively():
    _, cases = load_fixture_set()
    case = replace(cases[0], canary="CANARY-INJ-04-E5A8")
    ollama = _ollama(lambda request: _reply(_analysis(summary="Echoed canary-inj-04-e5a8 here.")))

    run = run_case(case, ollama, NullMeter(), Clock())

    assert run.unsupported == ("canary",)


def test_first_attempt_outcomes_are_recorded_per_reason():
    _, cases = load_fixture_set()
    bad_runbook = _analysis(runbook="nope#x")
    bad_evidence = {**_analysis(), "evidence": ["F99"]}
    for reply, expected in ((bad_runbook, "unknown_runbook"), (bad_evidence, "unknown_evidence")):
        ollama = _ollama(lambda request, reply=reply: _reply(reply))
        run = run_case(cases[0], ollama, NullMeter(), Clock())
        assert run.first_attempt == expected


def test_unreachable_first_attempt_then_valid_answer_is_not_valid_first_try():
    _, cases = load_fixture_set()
    replies = iter([httpx.ConnectError("refused"), _analysis()])

    def handler(request):
        item = next(replies)
        if isinstance(item, Exception):
            raise item
        return _reply(item)

    run = run_case(cases[0], _ollama(handler), NullMeter(), Clock())

    assert run.first_attempt == "unreachable"
    assert run.answer == Answer("C1", "vps-edge#upstream-502")


def _summary_runs(*outcomes):
    return [
        {
            "case_id": case_id,
            "answer": None,
            "first_attempt": outcome,
            "unavailable": None,
            "latency_s": 1.0,
            "cpu_s": None,
            "ram_peak_mib": None,
            "unsupported": [],
        }
        for case_id, outcome in zip(("app-01-cors", "none-01-quiet"), outcomes, strict=True)
    ]


def test_schema_rate_excludes_timeouts_and_unreachable_from_the_denominator():
    _, cases = load_fixture_set()

    summary = candidate_summary(cases, _summary_runs("valid", "timed out"))
    assert summary["schema_valid_first_pct"] == 100.0
    assert (summary["first_attempt_timeouts"], summary["first_attempt_unreachable"]) == (1, 0)

    summary = candidate_summary(cases, _summary_runs("schema", "unreachable"))
    assert summary["schema_valid_first_pct"] == 0.0
    assert summary["first_attempt_unreachable"] == 1

    summary = candidate_summary(cases, _summary_runs("timed out", "unreachable"))
    assert summary["schema_valid_first_pct"] == 0.0


def test_validated_analysis_and_comment_are_kept():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis()))

    run = run_case(cases[0], ollama, NullMeter(), Clock())

    assert run.analysis == _analysis()
    assert "The proxy drops requests." in run.comment
    assert run.rejection is None


def test_rejected_case_keeps_the_code_but_no_analysis():
    _, cases = load_fixture_set()
    ollama = _ollama(lambda request: _reply(_analysis(change="C42")))

    run = run_case(cases[0], ollama, NullMeter(), Clock())

    assert (run.analysis, run.comment, run.rejection) == (None, None, "unknown_change")


def test_cli_run_passes_the_attempt_timeout_to_ollama(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"]["read"])
        return _reply(_analysis())

    _patch_client(monkeypatch, _chat_only(handler))

    _main("run", "--model", "m", "--only", "app-01-cors", "--out", str(tmp_path / "a.json"))
    _main(
        "run", "--model", "m", "--only", "app-01-cors", "--timeout", "900",
        "--out", str(tmp_path / "b.json"),
    )  # fmt: skip

    assert seen == [300, 900]


def test_cases_over_the_production_timeout_are_counted():
    _, cases = load_fixture_set()
    runs = _summary_runs("valid", "timed out")
    runs[0]["latency_s"] = 301.0
    runs[1]["latency_s"] = 10.0
    runs[1]["unavailable"] = "model timed out"

    assert candidate_summary(cases, runs)["over_production_timeout"] == 2

    runs[1]["unavailable"] = None
    assert candidate_summary(cases, runs)["over_production_timeout"] == 1


def test_a_crash_after_case_k_leaves_k_cases_on_disk(tmp_path):
    _, cases = load_fixture_set()
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("runner died")
        return _reply(_analysis())

    out = tmp_path / "run.json"

    with pytest.raises(RuntimeError):
        run_candidate(cases, _ollama(handler), NullMeter(), "m", out=out)

    data = json.loads(out.read_text(encoding="utf-8"))
    assert [run["case_id"] for run in data["runs"]] == [cases[0].id]
    assert data["model"] == "m"
    assert list(tmp_path.iterdir()) == [out]


def test_finished_run_file_holds_every_case(tmp_path):
    _, cases = load_fixture_set()
    out = tmp_path / "run.json"

    result = run_candidate(cases, _ollama(lambda r: _reply(_analysis())), NullMeter(), "m", out=out)

    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(json.dumps(result))


def test_cli_run_prints_one_line_per_case(tmp_path, monkeypatch, capsys):
    _patch_client(monkeypatch, lambda request: _reply(_analysis()))

    _main("run", "--model", "m", "--out", str(tmp_path / "run.json"), "--container", "")

    lines = capsys.readouterr().out.splitlines()
    tokens = lines[0].split()
    assert tokens[:3] == ["app-01-cors", "C1", "vps-edge#upstream-502"]
    assert tokens[3].endswith("s")
    assert tokens[4:] == ["valid", "cpu", "n/a", "ram", "n/a"]
    assert lines[1].startswith("none-01-quiet ")
    assert lines[2].endswith("results in " + str(tmp_path / "run.json"))


def test_cli_run_line_names_the_reason_for_an_unavailable_case(tmp_path, monkeypatch, capsys):
    _patch_client(monkeypatch, lambda request: _reply("not json"))

    _main("run", "--model", "m", "--only", "app-01-cors", "--out", str(tmp_path / "run.json"))

    line = capsys.readouterr().out.splitlines()[0]
    assert "unavailable (invalid response)" in line
    assert "schema" in line


def _chat_only(handler):
    # The provenance lookups also hit the transport; the tests count chat calls only.
    def wrapper(request):
        if request.url.path != "/api/chat":
            return httpx.Response(404)
        return handler(request)

    return wrapper


def _patch_client(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("evals.__main__._client", lambda: client)


def _exit_code(tmp_path, monkeypatch, handler):
    _patch_client(monkeypatch, _chat_only(handler))
    return _main("run", "--model", "m", "--out", str(tmp_path / "run.json"))


def test_exit_code_is_zero_for_a_clean_run(tmp_path, monkeypatch):
    assert _exit_code(tmp_path, monkeypatch, lambda request: _reply(_analysis())) == 0


def test_exit_code_fails_on_an_unsupported_statement(tmp_path, monkeypatch):
    reply = _reply(_analysis(summary="Open https:// now."))

    assert _exit_code(tmp_path, monkeypatch, lambda request: reply) == 1


def test_exit_code_fails_when_the_model_is_unreachable(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= 2:
            raise httpx.ConnectError("refused")
        return _reply(_analysis())

    assert _exit_code(tmp_path, monkeypatch, handler) == 1


def test_exit_code_fails_when_every_case_is_unavailable(tmp_path, monkeypatch):
    assert _exit_code(tmp_path, monkeypatch, lambda request: _reply("not json")) == 1


def test_exit_code_tolerates_an_invalid_response_next_to_an_answer(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return _reply(_analysis() if len(calls) == 1 else "not json")

    assert _exit_code(tmp_path, monkeypatch, handler) == 0


def test_exit_code_tolerates_a_timeout_next_to_an_answer(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) > 1:
            raise httpx.ReadTimeout("slow")
        return _reply(_analysis())

    assert _exit_code(tmp_path, monkeypatch, handler) == 0


def _provenance_handler(tags=None, version=None):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags) if tags is not None else httpx.Response(500)
        if request.url.path == "/api/version":
            return httpx.Response(200, json=version) if version is not None else httpx.Response(500)
        return _reply(_analysis())

    return handler


def test_provenance_reads_digest_and_version():
    handler = _provenance_handler(
        tags={"models": [{"name": "other", "digest": "x"}, {"name": "m", "digest": "abc123"}]},
        version={"version": "0.40.12"},
    )

    result = fetch_provenance(httpx.Client(transport=httpx.MockTransport(handler)), "http://o", "m")

    assert result == {"model_digest": "abc123", "ollama_version": "0.40.12"}


def test_provenance_is_null_when_the_endpoints_fail_or_the_model_is_unknown():
    failing = _provenance_handler()
    unknown = _provenance_handler(tags={"models": []}, version={"nope": 1})
    junk = _provenance_handler(tags=["x"], version="1")

    for handler in (failing, unknown, junk):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert fetch_provenance(client, "http://o", "m") == {
            "model_digest": None,
            "ollama_version": None,
        }


def test_provenance_is_null_when_ollama_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert fetch_provenance(client, "http://o", "m") == {
        "model_digest": None,
        "ollama_version": None,
    }


def test_cli_run_records_provenance(tmp_path, monkeypatch):
    handler = _provenance_handler(
        tags={"models": [{"name": "m", "digest": "abc123"}]}, version={"version": "0.40.12"}
    )
    _patch_client(monkeypatch, handler)
    out = tmp_path / "run.json"

    _main("run", "--model", "m", "--only", "app-01-cors", "--out", str(out))

    data = json.loads(out.read_text(encoding="utf-8"))
    assert (data["model_digest"], data["ollama_version"]) == ("abc123", "0.40.12")


def _run_file(tmp_path, monkeypatch, model="m", name="run.json"):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _reply(_analysis())))
    monkeypatch.setattr("evals.__main__._client", lambda: client)
    run_file = tmp_path / name
    _main("run", "--model", model, "--out", str(run_file))
    return run_file


def _report_with(tmp_path, run_file, *specs):
    args = ["report", "--runs", str(run_file), "--out-dir", str(tmp_path / "out")]
    for spec in specs:
        args += ["--latency-run", spec]
    return _main(*args)


def test_cli_latency_run_splits_on_the_last_equals_and_is_repeatable(tmp_path, monkeypatch):
    run_file = _run_file(tmp_path, monkeypatch, model="a=b:1b")
    other = _run_file(tmp_path, monkeypatch, model="c:1b", name="other.json")
    latency = tmp_path / "latency.json"
    latency.write_text('{"runs": [{"latency_s": 42.0}]}', encoding="utf-8")

    code = _main(
        "report", "--runs", str(run_file), str(other), "--out-dir", str(tmp_path / "out"),
        "--latency-run", f"a=b:1b={latency}", "--latency-run", f"c:1b={latency}",
    )  # fmt: skip

    report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert code == 0
    assert [c["vps_latency_p95_s"] for c in report["candidates"]] == [42.0, 42.0]


@pytest.mark.parametrize("spec", ["no-separator", "=file.json", "model:1b="])
def test_cli_latency_run_rejects_a_malformed_spec(tmp_path, monkeypatch, spec):
    run_file = _run_file(tmp_path, monkeypatch)

    with pytest.raises(SystemExit, match="MODEL=FILE"):
        _report_with(tmp_path, run_file, spec)


def test_cli_latency_run_rejects_an_unknown_model(tmp_path, monkeypatch):
    run_file = _run_file(tmp_path, monkeypatch)
    other = tmp_path / "other.json"
    other.write_text('{"model": "ghost", "runs": [{"latency_s": 1.0}]}', encoding="utf-8")

    with pytest.raises(SystemExit, match="no candidate run for: ghost"):
        _report_with(tmp_path, run_file, f"ghost={other}")


def test_cli_latency_run_rejects_an_unreadable_file(tmp_path, monkeypatch):
    run_file = _run_file(tmp_path, monkeypatch)

    with pytest.raises(SystemExit, match="Cannot read latency run"):
        _report_with(tmp_path, run_file, f"m={tmp_path / 'missing.json'}")


def test_cli_latency_run_rejects_an_empty_run(tmp_path, monkeypatch):
    run_file = _run_file(tmp_path, monkeypatch)
    empty = tmp_path / "empty.json"
    empty.write_text('{"model": "m", "runs": []}', encoding="utf-8")

    with pytest.raises(SystemExit, match="has no cases"):
        _report_with(tmp_path, run_file, f"m={empty}")


def test_p95_is_a_public_nearest_rank_helper():
    from evals.runner import p95

    assert p95([float(n) for n in range(1, 21)]) == 19.0
    assert p95([]) == 0.0
