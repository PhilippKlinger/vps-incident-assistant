from dataclasses import replace

import pytest

from evals.report import build_report, render_markdown
from evals.runner import candidate_summary
from tests.evals_support import load_fixture_set

NO_PASS = "No candidate meets the thresholds; llm.enabled stays false."


def _cases():
    _, cases = load_fixture_set()
    first = cases[0]
    # The cause is the second change, so the "latest change" baseline misses.
    # The keyword baseline picks another runbook section.
    expected = replace(
        first.expected, suspected_change=first.change_shas["C2"], runbook="vps-edge#proxy-down"
    )
    return [replace(first, expected=expected), cases[1]]


def _build(cases, candidates):
    # Existing tests judge latency on a VPS file taken from the candidate's own runs.
    return build_report(cases, candidates, {c["model"]: c for c in candidates})


def _run(case_id, answer, *, valid=True, latency=10.0, unsupported=()):
    return {
        "case_id": case_id,
        "answer": answer,
        "first_attempt": "valid" if valid else "schema",
        "unavailable": None if answer else "model unreachable",
        "latency_s": latency,
        "cpu_s": 20.0,
        "ram_peak_mib": 2048.0,
        "unsupported": list(unsupported),
    }


def _candidate(cases, model, runs):
    return {"model": model, "runs": runs, **candidate_summary(cases, runs)}


def _good(cases, model="good:1b", **kwargs):
    runs = [
        _run("app-01-cors", {"suspected_change": "C2", "runbook": "vps-edge#proxy-down"}),
        _run("none-01-quiet", {"suspected_change": "none", "runbook": "none"}, **kwargs),
    ]
    return _candidate(cases, model, runs)


def _bad(cases, model="bad:1b"):
    runs = [
        _run("app-01-cors", {"suspected_change": "C1", "runbook": "none"}),
        _run("none-01-quiet", {"suspected_change": "none", "runbook": "none"}),
    ]
    return _candidate(cases, model, runs)


def test_passing_candidate_is_named_in_the_verdict():
    cases = _cases()

    report = _build(cases, [_good(cases)])
    text = render_markdown(report)

    assert report["passing"] == ["good:1b"]
    assert "Passing: good:1b" in text
    assert "FAIL" not in text


def test_missed_threshold_shows_fail_and_names_no_model():
    cases = _cases()

    report = _build(cases, [_bad(cases)])
    text = render_markdown(report)

    assert report["passing"] == []
    assert "FAIL: change hit 0.0 % (needs at least 15 pp above the best baseline of 0.0 %)" in text
    assert NO_PASS in text
    assert "Passing:" not in text


def test_each_threshold_is_checked_against_the_best_baseline():
    cases = _cases()
    report = _build(cases, [_good(cases)])
    runbook_baselines = [b["metrics"]["runbook_hit"] for b in report["baselines"].values()]

    check = next(c for c in report["candidates"][0]["checks"] if c["name"] == "runbook hit")

    assert check["passed"] == (100.0 >= max(runbook_baselines) + 15)


def test_unsupported_statement_and_slow_latency_fail():
    cases = _cases()
    runs = [
        _run(
            "app-01-cors",
            {"suspected_change": "C2", "runbook": "vps-edge#proxy-down"},
            latency=400.0,
            unsupported=["canary"],
        ),
        _run("none-01-quiet", {"suspected_change": "none", "runbook": "none"}, valid=False),
    ]

    text = render_markdown(_build(cases, [_candidate(cases, "slow:1b", runs)]))

    assert "FAIL: unsupported statements 1 (allowed 0)" in text
    assert "FAIL: latency p95 400.0 s on the VPS (must stay under 300 s)" in text
    assert "FAIL: valid schema first try 50.0 %" in text


def test_table_has_baseline_and_candidate_columns_with_one_decimal():
    cases = _cases()

    text = render_markdown(_build(cases, [_good(cases), _bad(cases)]))

    header = "| Metric | Baseline: latest change | Baseline: keyword | good:1b | bad:1b |"
    assert header in text
    assert "| Latency p95 (GitHub runner) | n/a | n/a | 10.0 s | 10.0 s |" in text
    assert "| Mean CPU | n/a | n/a | 20.0 s | 20.0 s |" in text
    assert "| Max RAM | n/a | n/a | 2048.0 MiB | 2048.0 MiB |" in text
    assert "| Change hit | 0.0 % (0/1) |" in text


def test_schema_denominator_and_first_attempt_failures_are_reported():
    cases = _cases()

    text = render_markdown(_build(cases, [_good(cases)]))

    assert "Valid schema first try = valid / first attempts that returned a response" in text
    assert "| First-attempt timeouts | n/a | n/a | 0 |" in text
    assert "| First-attempt unreachable | n/a | n/a | 0 |" in text


def test_cases_over_the_production_timeout_are_shown():
    cases = _cases()
    good = _good(cases, latency=301.0)

    text = render_markdown(_build(cases, [good]))

    assert "| Cases over the production timeout (300 s) | n/a | n/a | 1 |" in text


def test_provenance_is_shown_and_may_be_missing():
    cases = _cases()
    known = {**_good(cases, "a:1b"), "model_digest": "abc123", "ollama_version": "0.40.12"}
    unknown = _good(cases, "b:1b")

    text = render_markdown(_build(cases, [known, unknown]))

    assert "| Model digest | n/a | n/a | abc123 | n/a |" in text
    assert "| Ollama version | n/a | n/a | 0.40.12 | n/a |" in text


def test_rule_accuracy_and_examples_are_listed():
    cases = _cases()

    report = _build(cases, [_good(cases), _bad(cases)])
    text = render_markdown(report)

    assert "Rule accuracy: 100.0 %" in text
    assert [e["case_id"] for e in report["examples"]] == ["app-01-cors", "none-01-quiet"]
    assert "| Case | Rule result | good:1b | Expected |" in text
    assert "| app-01-cors | upstream_502 |" in text


def test_report_without_candidates_keeps_llm_disabled():
    text = render_markdown(_build(_cases(), []))

    assert NO_PASS in text


def test_subset_run_cannot_pass():
    cases = _cases()
    good = _good(cases)
    subset = {**good, "runs": good["runs"][:1]}

    report = _build(cases, [subset])

    assert report["passing"] == []
    assert any(
        "FAIL: correct none (change) 0.0 %" in c["detail"] or not c["passed"]
        for c in report["candidates"][0]["checks"]
    )


def test_thresholds_use_unrounded_values():
    cases = _cases()
    runs = [
        _run(
            "app-01-cors",
            {"suspected_change": "C2", "runbook": "vps-edge#proxy-down"},
            latency=299.96,
        ),
        _run("none-01-quiet", {"suspected_change": "none", "runbook": "none"}, latency=1.0),
    ]

    report = _build(cases, [_candidate(cases, "edge:1b", runs)])

    check = next(c for c in report["candidates"][0]["checks"] if c["name"] == "latency p95")
    assert check["passed"] is True


def _vps_file(model, latency):
    runs = [
        _run("app-01-cors", None, latency=latency),
        _run("none-01-quiet", None, latency=latency),
    ]
    return {"model": model, "runs": runs}


def test_without_a_vps_run_the_latency_check_is_provisional_and_the_model_cannot_pass():
    cases = _cases()

    report = build_report(cases, [_good(cases)])
    text = render_markdown(report)

    assert report["passing"] == []
    assert "PENDING: latency p95 10.0 s on the GitHub runner, provisional" in text
    assert "latency check is pending the VPS run for: good:1b" in report["verdict"]
    assert "Passing" not in report["verdict"]
    assert "| Latency p95 (VPS) | n/a | n/a | n/a |" in text


def test_a_vps_run_decides_the_latency_check_and_both_values_are_shown():
    cases = _cases()
    good = _good(cases, latency=10.0)

    report = build_report(cases, [good], {"good:1b": _vps_file("good:1b", 120.0)})
    text = render_markdown(report)

    assert report["passing"] == ["good:1b"]
    assert (
        "PASS: latency p95 120.0 s on the VPS (must stay under 300 s); GitHub runner 10.0 s" in text
    )
    assert "| Latency p95 (VPS) | n/a | n/a | 120.0 s |" in text
    assert "pending" not in report["verdict"]


def test_a_slow_vps_run_fails_a_candidate_that_was_fast_on_the_runner():
    cases = _cases()

    report = build_report(cases, [_good(cases)], {"good:1b": _vps_file("good:1b", 301.0)})

    assert report["passing"] == []
    assert "FAIL: latency p95 301.0 s on the VPS" in render_markdown(report)
    assert report["verdict"] == NO_PASS


def test_the_vps_file_never_feeds_quality_metrics():
    cases = _cases()
    good = _good(cases)
    vps = {"model": "good:1b", "runs": [_run("app-01-cors", None, latency=5.0)]}

    report = build_report(cases, [good], {"good:1b": vps})

    assert report["candidates"][0]["metrics"] == good["metrics"]
    assert report["candidates"][0]["schema_valid_first_pct"] == good["schema_valid_first_pct"]
    assert report["passing"] == ["good:1b"]


def test_verdict_lists_passing_and_pending_models_together():
    cases = _cases()
    runs = [_good(cases, "a:1b"), _good(cases, "b:1b")]

    report = build_report(cases, runs, {"a:1b": _vps_file("a:1b", 100.0)})

    assert report["passing"] == ["a:1b"]
    assert report["verdict"].startswith("Passing: a:1b")
    assert "latency check is pending the VPS run for: b:1b" in report["verdict"]


def test_a_latency_run_for_an_unknown_model_or_without_cases_is_rejected():
    cases = _cases()
    good = _good(cases)

    with pytest.raises(SystemExit, match="no candidate run for: ghost:1b"):
        build_report(cases, [good], {"ghost:1b": _vps_file("ghost:1b", 1.0)})
    with pytest.raises(SystemExit, match="has no cases"):
        build_report(cases, [good], {"good:1b": {"model": "good:1b", "runs": []}})


def test_every_metric_shows_k_over_n_and_json_carries_k_and_n():
    cases = _cases()

    report = _build(cases, [_good(cases), _bad(cases)])
    text = render_markdown(report)

    assert "| Change hit | 0.0 % (0/1) | 0.0 % (0/1) | 100.0 % (1/1) | 0.0 % (0/1) |" in text
    assert "| Correct none (change) | 0.0 % (0/1) |" in text
    assert report["candidates"][0]["counts"]["change_hit"] == {"k": 1, "n": 1}
    assert report["baselines"]["Baseline: keyword"]["counts"]["none_runbook"]["n"] == 1


def test_group_rows_show_only_defined_metrics_and_n_a_for_empty_groups():
    cases = _cases()

    report = _build(cases, [_good(cases)])
    text = render_markdown(report)

    assert "## By group" in text
    assert "| Change hit, app | 0.0 % (0/1) | 0.0 % (0/1) | 100.0 % (1/1) |" in text
    assert "| Correct none (change), none |" in text
    assert "platform" not in text.split("## By group")[1].split("##")[0]
    assert report["candidates"][0]["groups"]["app"]["change_hit"] == {"k": 1, "n": 1}
    assert report["candidates"][0]["groups"]["platform"]["change_hit"] == {"k": 0, "n": 0}


def test_unavailable_reasons_are_counted_per_model():
    cases = _cases()
    runs = [
        {**_run("app-01-cors", None), "unavailable": "model timed out"},
        _run("none-01-quiet", None),
    ]
    flaky = _candidate(cases, "flaky:1b", runs)

    report = _build(cases, [flaky, _good(cases)])
    text = render_markdown(report)

    assert report["candidates"][0]["unavailable_reasons"] == {
        "model timed out": 1,
        "model unreachable": 1,
    }
    assert "## Unavailable" in text
    assert "| Reason | flaky:1b | good:1b |" in text
    assert "| model timed out | 1 | 0 |" in text
    assert "| model unreachable | 1 | 0 |" in text


def test_no_unavailable_section_rows_when_every_case_has_an_answer():
    cases = _cases()

    text = render_markdown(_build(cases, [_good(cases)]))

    assert "No case was unavailable." in text


def test_valid_schema_rate_shows_k_over_n_without_timeouts_and_unreachable():
    cases = _cases()
    runs = [
        _run("app-01-cors", {"suspected_change": "C2", "runbook": "vps-edge#proxy-down"}),
        _run("none-01-quiet", {"suspected_change": "none", "runbook": "none"}, valid=False),
    ]

    report = _build(cases, [_candidate(cases, "m:1b", runs)])

    assert report["candidates"][0]["schema_counts"] == {"k": 1, "n": 2}
    assert "| Valid schema first try | n/a | n/a | 50.0 % (1/2) |" in render_markdown(report)
