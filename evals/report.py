from collections import Counter

from evals.baselines import Answer, keyword_match, latest_change
from evals.cases import Case
from evals.runner import PRODUCTION_TIMEOUT_S, candidate_summary, p95
from evals.scoring import CaseScore, rule_accuracy, score, summarize
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
GROUPS = ("app", "platform", "none", "injection")
# Metric key, table label, attribute of CaseScore.
METRICS = (
    ("change_hit", "Change hit", "change_hit"),
    ("runbook_hit", "Runbook hit", "runbook_hit"),
    ("none_change", "Correct none (change)", "none_change_ok"),
    ("none_runbook", "Correct none (runbook)", "none_runbook_ok"),
)


def _counts(scores: list[CaseScore]) -> dict:
    counts = {}
    for key, _, attr in METRICS:
        applicable = [v for v in (getattr(s, attr) for s in scores) if v is not None]
        counts[key] = {"k": sum(applicable), "n": len(applicable)}
    return counts


def _detail(scores: list[CaseScore]) -> dict:
    return {
        "counts": _counts(scores),
        "groups": {g: _counts([s for s in scores if s.group == g]) for g in GROUPS},
    }


def _run_scores(cases: list[Case], runs: list[dict]) -> list[CaseScore]:
    by_id = {run["case_id"]: run for run in runs}
    # A case missing from the run counts as unavailable, as in candidate_summary.
    return [
        score(c, Answer(**by_id[c.id]["answer"]) if by_id.get(c.id, {}).get("answer") else None)
        for c in cases
    ]


def _check(name: str, passed: bool, detail: str, provisional: bool = False) -> dict:
    return {"name": name, "passed": passed, "detail": detail, "provisional": provisional}


def _latency_check(candidate: dict) -> dict:
    runner = candidate["latency_p95_s"]
    vps = candidate["vps_latency_p95_s"]
    if vps is None:
        # The VPS run is authoritative (spec section 12); the runner value cannot pass a model.
        return _check(
            "latency p95",
            False,
            f"latency p95 {runner:.1f} s on the GitHub runner, provisional "
            "(the VPS run decides this check)",
            provisional=True,
        )
    return _check(
        "latency p95",
        vps < LATENCY_P95_MAX_S,
        f"latency p95 {vps:.1f} s on the VPS (must stay under {LATENCY_P95_MAX_S} s); "
        f"GitHub runner {runner:.1f} s",
    )


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
    checks.append(_latency_check(candidate))
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


def _vps_p95(model: str, latency_run: dict) -> float:
    runs = latency_run["runs"]
    if not runs:
        raise SystemExit(f"Latency run for {model} has no cases")
    # A sample is enough here; quality metrics never read this file.
    return p95([run["latency_s"] for run in runs])


def _verdict(passing: list[str], pending: list[str]) -> str:
    note = f"latency check is pending the VPS run for: {', '.join(pending)}"
    if passing:
        text = f"Passing: {', '.join(passing)}"
        return f"{text}; {note}" if pending else text
    if pending:
        return f"No candidate has passed yet; {note}. llm.enabled stays false."
    return NO_PASS


def build_report(
    cases: list[Case], runs: list[dict], latency_runs: dict[str, dict] | None = None
) -> dict:
    latency_runs = latency_runs or {}
    unknown = sorted(latency_runs.keys() - {run["model"] for run in runs})
    if unknown:
        raise SystemExit(f"Latency run given, but no candidate run for: {', '.join(unknown)}")
    baseline_scores = {name: [score(c, fn(c)) for c in cases] for name, fn in BASELINES.items()}
    baselines = {
        name: {"metrics": dict(vars(summarize(scores, digits=None))), **_detail(scores)}
        for name, scores in baseline_scores.items()
    }
    best = {
        key: max(b["metrics"][key] for b in baselines.values())
        for key in ("change_hit", "runbook_hit")
    }
    candidates = []
    for run in runs:
        candidate = {**run, **candidate_summary(cases, run["runs"])}
        candidate.update(_detail(_run_scores(cases, run["runs"])))
        outcomes = [r["first_attempt"] for r in run["runs"]]
        responded = len(outcomes) - outcomes.count("timed out") - outcomes.count("unreachable")
        candidate["schema_counts"] = {"k": outcomes.count("valid"), "n": responded}
        candidate["unavailable_reasons"] = dict(
            Counter(r["unavailable"] for r in run["runs"] if r["unavailable"])
        )
        model = run["model"]
        candidate["vps_latency_p95_s"] = (
            _vps_p95(model, latency_runs[model]) if model in latency_runs else None
        )
        candidate["checks"] = _checks(candidate, best)
        candidate["passes"] = all(c["passed"] for c in candidate["checks"])
        candidates.append(candidate)
    passing = [c["model"] for c in candidates if c["passes"]]
    pending = [
        c["model"]
        for c in candidates
        if all(k["passed"] or k["provisional"] for k in c["checks"])
        and any(k["provisional"] for k in c["checks"])
    ]
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
        "verdict": _verdict(passing, pending),
        "rule_accuracy": rule_accuracy(cases),
        "best_candidate": leader["model"] if leader else None,
        "examples": _examples(cases, leader),
    }


def _status(check: dict) -> str:
    if check["provisional"]:
        return "PENDING"
    return "PASS" if check["passed"] else "FAIL"


def _pct(value: float) -> str:
    return f"{value:.1f} %"


def _rate(value: float, count: dict) -> str:
    return f"{_pct(value)} ({count['k']}/{count['n']})"


def _group_cell(count: dict) -> str:
    if count["n"] == 0:
        return NA
    return f"{_pct(100 * count['k'] / count['n'])} ({count['k']}/{count['n']})"


def _optional(value: float | None, unit: str) -> str:
    return NA if value is None else f"{value:.1f} {unit}"


def _row(label: str, baseline_cell: str | None, candidate_cell, report: dict) -> str:
    cells = [baseline_cell or NA for _ in report["baselines"]]
    cells += [candidate_cell(c) for c in report["candidates"]]
    return f"| {label} | " + " | ".join(cells) + " |"


def _table(report: dict) -> list[str]:
    header = ["Metric", *report["baselines"], *(c["model"] for c in report["candidates"])]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for key, label, _ in METRICS:
        columns = [*report["baselines"].values(), *report["candidates"]]
        cells = [_rate(c["metrics"][key], c["counts"][key]) for c in columns]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += [
        _row("Model digest", None, lambda c: c.get("model_digest") or NA, report),
        _row("Ollama version", None, lambda c: c.get("ollama_version") or NA, report),
        _row(
            "Valid schema first try",
            None,
            lambda c: _rate(c["schema_valid_first_pct"], c["schema_counts"]),
            report,
        ),
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
        _row(
            "Latency p95 (GitHub runner)",
            None,
            lambda c: _optional(c["latency_p95_s"], "s"),
            report,
        ),
        _row("Latency p95 (VPS)", None, lambda c: _optional(c["vps_latency_p95_s"], "s"), report),
        _row("Mean CPU", None, lambda c: _optional(c["cpu_s_mean"], "s"), report),
        _row("Max RAM", None, lambda c: _optional(c["ram_peak_mib_max"], "MiB"), report),
    ]
    return lines


def _group_table(report: dict) -> list[str]:
    columns = [*report["baselines"].values(), *report["candidates"]]
    header = ["Metric, group", *report["baselines"], *(c["model"] for c in report["candidates"])]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for key, label, _ in METRICS:
        for group in GROUPS:
            counts = [c["groups"][group][key] for c in columns]
            # A metric a group never has (n = 0 everywhere) would only add n/a rows.
            if any(count["n"] for count in counts):
                cells = [_group_cell(count) for count in counts]
                lines.append(f"| {label}, {group} | " + " | ".join(cells) + " |")
    return lines


def _unavailable_table(report: dict) -> list[str]:
    candidates = report["candidates"]
    reasons = sorted({r for c in candidates for r in c["unavailable_reasons"]})
    if not reasons:
        return ["No case was unavailable."]
    header = ["Reason", *(c["model"] for c in candidates)]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for reason in reasons:
        cells = [str(c["unavailable_reasons"].get(reason, 0)) for c in candidates]
        lines.append(f"| {reason} | " + " | ".join(cells) + " |")
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
        "CPU and RAM are measured per case and have no threshold. "
        "Rates show hits / applicable cases (k/n).",
        "",
        "## By group",
        "",
        *_group_table(report),
        "",
        "## Unavailable",
        "",
        *_unavailable_table(report),
        "",
        "## Thresholds",
    ]
    for candidate in report["candidates"]:
        lines += ["", f"### {candidate['model']}", ""]
        lines += [f"- {_status(c)}: {c['detail']}" for c in candidate["checks"]]
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
