import json
from dataclasses import asdict, replace

import httpx
import pytest

from assistant.llm import BedrockClient
from evals.baselines import Answer
from evals.runner import candidate_summary, describe_run, run_candidate, run_case
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


def _llm(handler) -> BedrockClient:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BedrockClient(client, "eu-north-1", "m", "secret-token")


def _reply(content, usage=(7, 3)) -> httpx.Response:
    # A string stands for a tool input the schema rejects.
    tool_input = {"junk": content} if isinstance(content, str) else content
    block = {"toolUse": {"toolUseId": "t1", "name": "record_analysis", "input": tool_input}}
    return httpx.Response(
        200,
        json={
            "output": {"message": {"role": "assistant", "content": [block]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": usage[0], "outputTokens": usage[1]},
        },
    )


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 2.0
        return self.now


def test_valid_answer_is_recorded():
    _, cases = load_fixture_set()
    llm = _llm(lambda request: _reply(_analysis()))

    run = run_case(cases[0], llm, Clock())

    assert run.case_id == "app-01-cors"
    assert run.answer == Answer("C1", "vps-edge#upstream-502")
    assert run.first_attempt == "valid"
    assert run.unavailable is None
    assert (run.input_tokens, run.output_tokens) == (7, 3)
    assert run.latency_s == 2.0
    assert run.unsupported == ()


def test_tokens_are_summed_over_the_attempts_of_a_case():
    _, cases = load_fixture_set()
    replies = iter([_reply(_analysis(change="C42"), usage=(100, 20)), _reply(_analysis())])
    llm = _llm(lambda request: next(replies))

    run = run_case(cases[0], llm, Clock())

    assert (run.input_tokens, run.output_tokens) == (107, 23)


def test_invalid_then_valid_answer_is_not_valid_first_try():
    _, cases = load_fixture_set()
    replies = iter(["not json", _analysis()])
    llm = _llm(lambda request: _reply(next(replies)))

    run = run_case(cases[0], llm, Clock())

    assert run.first_attempt == "schema"
    assert run.answer == Answer("C1", "vps-edge#upstream-502")


def test_unknown_change_id_counts_as_invalid():
    _, cases = load_fixture_set()
    llm = _llm(lambda request: _reply(_analysis(change="C42")))

    run = run_case(cases[0], llm, Clock())

    assert (run.answer, run.unavailable) == (None, "invalid response")
    assert (run.first_attempt, run.rejection) == ("unknown_change", "unknown_change")


def test_http_status_of_the_failed_attempt_is_recorded():
    _, cases = load_fixture_set()

    run = run_case(cases[0], _llm(lambda r: httpx.Response(429)))

    assert run.unavailable == "model unreachable"
    assert run.http_status == 429
    assert "http 429" in describe_run(asdict(run))


def test_unreachable_model_is_recorded_and_the_run_continues():
    _, cases = load_fixture_set()
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= 2:
            raise httpx.ConnectError("refused")
        return _reply(_analysis(change="none", runbook="none"))

    llm = _llm(handler)

    result = run_candidate(cases, llm, "m")

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

    run = run_case(cases[0], _llm(handler), Clock())

    assert run.unavailable == "model timed out"
    assert run.first_attempt == "timed out"
    assert run.latency_s == 2.0  # the clock is read once before and once after both attempts


def test_canary_in_the_published_comment_is_unsupported():
    _, cases = load_fixture_set()
    case = replace(cases[0], canary="CANARY-7731")
    llm = _llm(lambda request: _reply(_analysis(summary="Leaked CANARY-7731 here.")))

    run = run_case(case, llm, Clock())

    assert run.unsupported == ("canary",)


def test_the_comment_own_runbook_link_is_not_a_violation():
    _, cases = load_fixture_set()
    llm = _llm(lambda request: _reply(_analysis()))

    run = run_case(cases[0], llm, Clock())

    assert run.unsupported == ()


def test_candidate_summary():
    _, cases = load_fixture_set()
    llm = _llm(lambda request: _reply(_analysis(change="C1", runbook="vps-edge#upstream-502")))

    result = run_candidate(cases, llm, "m")

    assert result["model"] == "m"
    assert len(result["runs"]) == 2
    assert result["metrics"]["change_hit"] == 100.0
    assert result["metrics"]["none_change"] == 0.0
    assert result["schema_valid_first_pct"] == 100.0
    assert (result["first_attempt_timeouts"], result["first_attempt_unreachable"]) == (0, 0)
    assert result["unsupported_count"] == 0
    assert result["latency_p95_s"] >= 0
    assert result["input_tokens_total"] == 14
    assert result["output_tokens_total"] == 6
    assert (result["input_tokens_median"], result["output_tokens_median"]) == (7, 3)


@pytest.fixture(autouse=True)
def _bedrock_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_REGION", "eu-north-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "secret-token")


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


def test_cli_candidates_prints_the_bedrock_matrix(capsys):
    assert _main("candidates") == 0

    data = json.loads(capsys.readouterr().out)
    assert [c["model"] for c in data["matrix"]] == [
        "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        "eu.anthropic.claude-sonnet-4-6",
    ]


def test_underscore_canary_is_found_after_sanitizing():
    _, cases = load_fixture_set()
    case = replace(cases[0], canary="CANARY_7731")
    llm = _llm(lambda request: _reply(_analysis(summary="Leaked CANARY_7731 here.")))

    run = run_case(case, llm, Clock())

    assert run.unsupported == ("canary",)


def test_missing_cases_count_as_unavailable():
    _, cases = load_fixture_set()
    llm = _llm(lambda request: _reply(_analysis()))
    full = run_candidate(cases, llm, "m")

    summary = candidate_summary(cases, full["runs"][:1])

    # app-01-cors answered correctly; none-01-quiet is missing, so wrong.
    assert summary["metrics"]["change_hit"] == 100.0
    assert summary["metrics"]["none_change"] == 0.0
    # Only cases that were run count for the schema rate.
    assert summary["schema_valid_first_pct"] == 100.0


def test_run_with_unknown_case_ids_is_rejected():
    _, cases = load_fixture_set()
    llm = _llm(lambda request: _reply(_analysis()))
    runs = run_candidate(cases, llm, "m")["runs"]
    runs[0] = {**runs[0], "case_id": "ghost"}

    with pytest.raises(SystemExit, match="Unknown case ids in run: ghost"):
        candidate_summary(cases, runs)


def test_canary_is_found_case_insensitively():
    _, cases = load_fixture_set()
    case = replace(cases[0], canary="CANARY-INJ-04-E5A8")
    llm = _llm(lambda request: _reply(_analysis(summary="Echoed canary-inj-04-e5a8 here.")))

    run = run_case(case, llm, Clock())

    assert run.unsupported == ("canary",)


def test_first_attempt_outcomes_are_recorded_per_reason():
    _, cases = load_fixture_set()
    bad_runbook = _analysis(runbook="nope#x")
    bad_evidence = {**_analysis(), "evidence": ["F99"]}
    for reply, expected in ((bad_runbook, "unknown_runbook"), (bad_evidence, "unknown_evidence")):
        llm = _llm(lambda request, reply=reply: _reply(reply))
        run = run_case(cases[0], llm, Clock())
        assert run.first_attempt == expected


def test_unreachable_first_attempt_then_valid_answer_is_not_valid_first_try():
    _, cases = load_fixture_set()
    replies = iter([httpx.ConnectError("refused"), _analysis()])

    def handler(request):
        item = next(replies)
        if isinstance(item, Exception):
            raise item
        return _reply(item)

    run = run_case(cases[0], _llm(handler), Clock())

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
            "input_tokens": 0,
            "output_tokens": 0,
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
    llm = _llm(lambda request: _reply(_analysis()))

    run = run_case(cases[0], llm, Clock())

    assert run.analysis == _analysis()
    assert "The proxy drops requests." in run.comment
    assert run.rejection is None


def test_rejected_case_keeps_the_code_but_no_analysis():
    _, cases = load_fixture_set()
    llm = _llm(lambda request: _reply(_analysis(change="C42")))

    run = run_case(cases[0], llm, Clock())

    assert (run.analysis, run.comment, run.rejection) == (None, None, "unknown_change")


def test_rejected_replies_are_kept_raw_and_cut():
    _, cases = load_fixture_set()
    rejected = json.dumps(_analysis(change="C42"))
    replies = iter([_analysis(change="C42"), "x" * 5000, _analysis()])
    llm = _llm(lambda request: _reply(next(replies)))

    run = run_case(cases[0], llm, Clock())

    # Both attempts were rejected (unknown id, then no JSON); each raw reply is kept, cut.
    assert run.answer is None
    assert run.rejected_raw == (rejected, json.dumps({"junk": "x" * 5000})[:2000])


def test_valid_first_answer_keeps_no_raw_reply():
    _, cases = load_fixture_set()
    llm = _llm(lambda request: _reply(_analysis()))

    run = run_case(cases[0], llm, Clock())

    assert run.rejected_raw == ()


def test_cli_run_passes_the_attempt_timeout_to_llm(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"]["read"])
        return _reply(_analysis())

    _patch_client(monkeypatch, handler)

    _main("run", "--model", "m", "--only", "app-01-cors", "--out", str(tmp_path / "a.json"))
    _main(
        "run", "--model", "m", "--only", "app-01-cors", "--timeout", "900",
        "--out", str(tmp_path / "b.json"),
    )  # fmt: skip

    assert seen == [60, 900]


def test_cases_over_the_production_timeout_are_counted():
    _, cases = load_fixture_set()
    runs = _summary_runs("valid", "timed out")
    runs[0]["latency_s"] = 61.0
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
        run_candidate(cases, _llm(handler), "m", out=out)

    data = json.loads(out.read_text(encoding="utf-8"))
    assert [run["case_id"] for run in data["runs"]] == [cases[0].id]
    assert data["model"] == "m"
    assert list(tmp_path.iterdir()) == [out]


def test_finished_run_file_holds_every_case(tmp_path):
    _, cases = load_fixture_set()
    out = tmp_path / "run.json"

    result = run_candidate(cases, _llm(lambda r: _reply(_analysis())), "m", out=out)

    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(json.dumps(result))


def test_cli_run_prints_one_line_per_case(tmp_path, monkeypatch, capsys):
    _patch_client(monkeypatch, lambda request: _reply(_analysis()))

    _main("run", "--model", "m", "--out", str(tmp_path / "run.json"))

    lines = capsys.readouterr().out.splitlines()
    tokens = lines[0].split()
    assert tokens[:3] == ["app-01-cors", "C1", "vps-edge#upstream-502"]
    assert tokens[3].endswith("s")
    assert tokens[4:] == ["valid", "in", "7", "out", "3"]
    assert lines[1].startswith("none-01-quiet ")
    assert lines[2].endswith("results in " + str(tmp_path / "run.json"))


def test_cli_run_line_names_the_reason_for_an_unavailable_case(tmp_path, monkeypatch, capsys):
    _patch_client(monkeypatch, lambda request: _reply("not json"))

    _main("run", "--model", "m", "--only", "app-01-cors", "--out", str(tmp_path / "run.json"))

    line = capsys.readouterr().out.splitlines()[0]
    assert "unavailable (invalid response)" in line
    assert "schema" in line


def _patch_client(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("evals.__main__._client", lambda: client)


def _exit_code(tmp_path, monkeypatch, handler):
    _patch_client(monkeypatch, handler)
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


def test_cli_run_records_the_region(tmp_path, monkeypatch):
    _patch_client(monkeypatch, lambda request: _reply(_analysis()))
    out = tmp_path / "run.json"

    _main("run", "--model", "m", "--only", "app-01-cors", "--out", str(out))

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["region"] == "eu-north-1"
    assert "secret-token" not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("missing", ["BEDROCK_REGION", "AWS_BEARER_TOKEN_BEDROCK"])
def test_cli_run_needs_region_and_token(tmp_path, monkeypatch, missing):
    monkeypatch.delenv(missing)

    with pytest.raises(SystemExit, match="BEDROCK_REGION and AWS_BEARER_TOKEN_BEDROCK") as error:
        _main("run", "--model", "m", "--out", str(tmp_path / "run.json"))

    assert "secret-token" not in str(error.value)


def test_p95_is_a_public_nearest_rank_helper():
    from evals.runner import p95

    assert p95([float(n) for n in range(1, 21)]) == 19.0
    assert p95([]) == 0.0
