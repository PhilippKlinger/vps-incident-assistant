from evals.baselines import Answer, keyword_match, latest_change
from evals.cases import Case
from evals.runner import PRODUCTION_TIMEOUT_S, candidate_summary
from evals.scoring import rule_accuracy, score, summarize
from evals.thresholds import (
    CHANGE_MARGIN_PP,
    LATENCY_P95_MAX_S,
    NONE_MIN_PCT,
    RUNBOOK_MARGIN_PP,
    SCHEMA_MIN_PCT,
    UNSUPPORTED_MAX,
)

BASELINES = {"Baseline: latest change": latest_change, "Baseline: keyword": keyword_match}
NO_PASS = "No candidate meets the thresholds; llm.enabled stays false."
EXAMPLES = 3
NA = "n/a"


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def _checks(candidate: dict, best: dict) -> list[dict]:
    m = candidate["metrics"]
    checks = []
    for key, name, margin in (
        ("change_hit", "change hit", CHANGE_MARGIN_PP),
        ("runbook_hit", "runbook hit", RUNBOOK_MARGIN_PP),
    ):
        checks.append(
            _check(
                name,
                m[key] >= best[key] + margin,
                f"{name} {m[key]:.1f} % (needs at least {margin} pp above the best baseline "
                f"of {best[key]:.1f} %)",
            )
        )
    for key, name in (
        ("none_change", "correct none (change)"),
        ("none_runbook", "correct none (runbook)"),
    ):
        checks.append(
            _check(
                name,
                m[key] >= NONE_MIN_PCT,
                f"{name} {m[key]:.1f} % (needs at least {NONE_MIN_PCT} %)",
            )
        )
    schema = candidate["schema_valid_first_pct"]
    checks.append(
        _check(
            "valid schema first try",
            schema >= SCHEMA_MIN_PCT,
            f"valid schema first try {schema:.1f} % (needs at least {SCHEMA_MIN_PCT} %)",
        )
    )
    unsupported = candidate["unsupported_count"]
    checks.append(
        _check(
            "unsupported statements",
            unsupported <= UNSUPPORTED_MAX,
            f"unsupported statements {unsupported} (allowed {UNSUPPORTED_MAX})",
        )
    )
    latency = candidate["latency_p95_s"]
    checks.append(
        _check(
            "latency p95",
            latency < LATENCY_P95_MAX_S,
            f"latency p95 {latency:.1f} s (must stay under {LATENCY_P95_MAX_S} s)",
        )
    )
    return checks


def _fully_correct(case: Case, answer: dict | None) -> bool:
    result = score(case, Answer(**answer) if answer else None)
    flags = [result.change_hit, result.runbook_hit, result.none_change_ok, result.none_runbook_ok]
    return all(flag for flag in flags if flag is not None)


def _describe(case: Case, change: str, runbook: str) -> str:
    sha = case.change_shas.get(change)
    if change == "none":
        change_text = "none"
    else:
        change_text = f"{change} ({sha[:7]})" if sha else change
    return f"change {change_text}, runbook {runbook}"


def _examples(cases: list[Case], best: dict | None) -> list[dict]:
    if best is None:
        return []
    answers = {run["case_id"]: run for run in best["runs"]}
    cases = [case for case in cases if case.id in answers]
    right = [c for c in cases if _fully_correct(c, answers[c.id]["answer"])]
    wrong = [c for c in cases if c not in right]
    injection = [c for c in cases if c.group == "injection"]
    picked: list[Case] = []
    # One right, one wrong and one injection case show the range; the rest fills up.
    for case in [*right[:1], *wrong[:1], *injection[:1], *cases]:
        if case not in picked and len(picked) < EXAMPLES:
            picked.append(case)
    examples = []
    for case in sorted(picked, key=lambda c: c.id):
        run = answers[case.id]
        answer = run["answer"]
        given = (
            _describe(case, answer["suspected_change"], answer["runbook"])
            if answer
            else f"unavailable ({run['unavailable']})"
        )
        expected = case.expected
        sha = expected.suspected_change
        expected_change = "none" if sha == "none" else sha[:7]
        examples.append(
            {
                "case_id": case.id,
                "group": case.group,
                "rule": case.context.rule.category,
                "candidate": given,
                "expected": f"change {expected_change}, runbook {expected.runbook}",
            }
        )
    return examples


def build_report(cases: list[Case], runs: list[dict]) -> dict:
    baselines = {
        name: {"metrics": dict(vars(summarize([score(c, fn(c)) for c in cases], digits=None)))}
        for name, fn in BASELINES.items()
    }
    best = {
        key: max(b["metrics"][key] for b in baselines.values())
        for key in ("change_hit", "runbook_hit")
    }
    candidates = []
    for run in runs:
        candidate = {**run, **candidate_summary(cases, run["runs"])}
        candidate["checks"] = _checks(candidate, best)
        candidate["passes"] = all(c["passed"] for c in candidate["checks"])
        candidates.append(candidate)
    passing = [c["model"] for c in candidates if c["passes"]]
    leader = max(
        candidates,
        key=lambda c: c["metrics"]["change_hit"] + c["metrics"]["runbook_hit"],
        default=None,
    )
    return {
        "cases": len(cases),
        "baselines": baselines,
        "candidates": candidates,
        "passing": passing,
        "verdict": f"Passing: {', '.join(passing)}" if passing else NO_PASS,
        "rule_accuracy": rule_accuracy(cases),
        "best_candidate": leader["model"] if leader else None,
        "examples": _examples(cases, leader),
    }


def _pct(value: float) -> str:
    return f"{value:.1f} %"


def _optional(value: float | None, unit: str) -> str:
    return NA if value is None else f"{value:.1f} {unit}"


def _row(label: str, baseline_cell: str | None, candidate_cell, report: dict) -> str:
    cells = [baseline_cell or NA for _ in report["baselines"]]
    cells += [candidate_cell(c) for c in report["candidates"]]
    return f"| {label} | " + " | ".join(cells) + " |"


def _table(report: dict) -> list[str]:
    header = ["Metric", *report["baselines"], *(c["model"] for c in report["candidates"])]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for label, key in (
        ("Change hit", "change_hit"),
        ("Runbook hit", "runbook_hit"),
        ("Correct none (change)", "none_change"),
        ("Correct none (runbook)", "none_runbook"),
    ):
        cells = [_pct(b["metrics"][key]) for b in report["baselines"].values()]
        cells += [_pct(c["metrics"][key]) for c in report["candidates"]]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += [
        _row("Model digest", None, lambda c: c.get("model_digest") or NA, report),
        _row("Ollama version", None, lambda c: c.get("ollama_version") or NA, report),
        _row("Valid schema first try", None, lambda c: _pct(c["schema_valid_first_pct"]), report),
        _row("First-attempt timeouts", None, lambda c: str(c["first_attempt_timeouts"]), report),
        _row(
            "First-attempt unreachable", None, lambda c: str(c["first_attempt_unreachable"]), report
        ),
        _row(
            f"Cases over the production timeout ({PRODUCTION_TIMEOUT_S} s)",
            None,
            lambda c: str(c["over_production_timeout"]),
            report,
        ),
        _row("Unsupported statements", None, lambda c: str(c["unsupported_count"]), report),
        _row("Latency p95", None, lambda c: _optional(c["latency_p95_s"], "s"), report),
        _row("Mean CPU", None, lambda c: _optional(c["cpu_s_mean"], "s"), report),
        _row("Max RAM", None, lambda c: _optional(c["ram_peak_mib_max"], "MiB"), report),
    ]
    return lines


def render_markdown(report: dict) -> str:
    lines = [
        "# Incident assistant eval report",
        "",
        f"{report['cases']} frozen cases. Thresholds are fixed in `evals/thresholds.py`; "
        "the two baselines are the most recent change and a keyword match.",
        "",
        f"**{report['verdict']}**",
        "",
        *_table(report),
        "",
        "Valid schema first try = valid / first attempts that returned a response; "
        "timeouts and unreachable are excluded and counted separately.",
        "",
        "CPU and RAM are measured per case and have no threshold.",
        "",
        "## Thresholds",
    ]
    for candidate in report["candidates"]:
        lines += ["", f"### {candidate['model']}", ""]
        lines += [
            f"- {'PASS' if c['passed'] else 'FAIL'}: {c['detail']}" for c in candidate["checks"]
        ]
    lines += ["", "## Rule engine", "", f"Rule accuracy: {_pct(report['rule_accuracy'])}"]
    if report["examples"]:
        lines += [
            "",
            "## Example cases",
            "",
            f"| Case | Rule result | {report['best_candidate']} | Expected |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {e['case_id']} | {e['rule']} | {e['candidate']} | {e['expected']} |"
            for e in report["examples"]
        ]
    return "\n".join(lines) + "\n"
