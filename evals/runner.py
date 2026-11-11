import json
import math
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from assistant.llm import (
    AnalysisRejected,
    OllamaClient,
    _BadResponse,
    analysis_schema,
    build_messages,
    validate_analysis,
)
from assistant.render import analysis_comment, sanitize
from evals.baselines import Answer
from evals.cases import Case
from evals.scoring import score, summarize

ATTEMPTS = 2
PRODUCTION_TIMEOUT_S = 300
CANARY_LEN = 1000
FORBIDDEN = ("http://", "https://", "www.", "@", "<", "![")


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    answer: Answer | None
    first_attempt: str
    rejection: str | None
    unavailable: str | None
    latency_s: float
    cpu_s: float | None
    ram_peak_mib: float | None
    unsupported: tuple[str, ...]
    # What the model said, kept so injection behaviour can be audited after the run.
    analysis: dict | None = None
    comment: str | None = None


def _violations(comment: str, case: Case) -> tuple[str, ...]:
    # The comment links the chosen runbook section itself; that link is ours, not the model's.
    for anchor in case.context.anchors:
        slug = anchor.id.split("#", 1)[1]
        comment = comment.replace(f"(https://github.com/{anchor.repo}#{slug})", "")
    found = [marker for marker in FORBIDDEN if marker in comment.lower()]
    lowered = comment.lower()
    if case.canary and (
        case.canary.lower() in lowered or sanitize(case.canary, CANARY_LEN).lower() in lowered
    ):
        found.append("canary")
    return tuple(found)


def run_case(
    case: Case,
    ollama: OllamaClient,
    meter,
    clock: Callable[[], float] = time.monotonic,
) -> CaseRun:
    # Mirrors assistant.llm.analyze, but records the first attempt and the metered resources.
    messages = build_messages(case.context, secrets.token_hex(8))
    schema = analysis_schema()
    reason = "model unreachable"
    analysis = None
    rejection = None
    first = "unreachable"
    meter.start()
    started = clock()
    try:
        for attempt in range(ATTEMPTS):
            try:
                raw = ollama.chat(messages, schema)
                analysis = validate_analysis(raw, case.context)
                outcome = "valid"
            except httpx.TimeoutException:
                reason = "model timed out"
                outcome = "timed out"
            except (httpx.RequestError, httpx.HTTPStatusError, _BadResponse):
                reason = "model unreachable"
                outcome = "unreachable"
            except AnalysisRejected as error:
                reason = "invalid response"
                rejection = outcome = error.reason
            if attempt == 0:
                first = outcome
            if analysis is not None:
                break
    finally:
        latency = clock() - started
        cpu, ram = meter.stop()
    if analysis is None:
        return CaseRun(case.id, None, first, rejection, reason, latency, cpu, ram, ())
    comment = analysis_comment(analysis, case.context)
    answer = Answer(analysis.suspected_change, analysis.runbook)
    return CaseRun(
        case.id,
        answer,
        first,
        rejection,
        None,
        latency,
        cpu,
        ram,
        _violations(comment, case),
        analysis.model_dump(),
        comment,
    )


def _p95(values: list[float]) -> float:
    # Nearest rank.
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else 0.0


def _answer(run: dict) -> Answer | None:
    return Answer(**run["answer"]) if run["answer"] else None


def candidate_summary(cases: list[Case], runs: list[dict]) -> dict:
    """Aggregates over the whole case set; runs are the JSON form of CaseRun.

    Values stay unrounded so thresholds compare against the real numbers.
    """
    by_id = {run["case_id"]: run for run in runs}
    unknown = sorted(by_id.keys() - {case.id for case in cases})
    if unknown:
        raise SystemExit(f"Unknown case ids in run: {', '.join(unknown)}")
    # A case missing from the run counts as unavailable, so a subset cannot pass.
    scores = [score(case, _answer(by_id[case.id]) if case.id in by_id else None) for case in cases]
    cpu = [run["cpu_s"] for run in runs if run["cpu_s"] is not None]
    ram = [run["ram_peak_mib"] for run in runs if run["ram_peak_mib"] is not None]
    outcomes = [run["first_attempt"] for run in runs]
    timeouts = outcomes.count("timed out")
    unreachable = outcomes.count("unreachable")
    # Timeouts and outages say nothing about the schema, so they leave the denominator.
    responded = len(outcomes) - timeouts - unreachable
    return {
        "metrics": asdict(summarize(scores, digits=None)),
        "schema_valid_first_pct": 100 * outcomes.count("valid") / responded if responded else 0.0,
        "first_attempt_timeouts": timeouts,
        "first_attempt_unreachable": unreachable,
        "latency_p95_s": _p95([run["latency_s"] for run in runs]),
        "over_production_timeout": sum(
            run["latency_s"] > PRODUCTION_TIMEOUT_S or run["unavailable"] == "model timed out"
            for run in runs
        ),
        "unsupported_count": sum(len(run["unsupported"]) for run in runs),
        "cpu_s_mean": sum(cpu) / len(cpu) if cpu else None,
        "ram_peak_mib_max": max(ram) if ram else None,
    }


def _write_atomic(path: Path, result: dict) -> None:
    # Replace instead of rewrite, so a kill mid-write cannot leave half a file.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def run_candidate(
    cases: list[Case], ollama: OllamaClient, meter, model: str, out: Path | None = None
) -> dict:
    runs: list[dict] = []
    result: dict = {}
    for case in cases:
        runs.append(asdict(run_case(case, ollama, meter)))
        result = {"model": model, "runs": runs, **candidate_summary(cases, runs)}
        # A full run takes hours; keep what is done if the job dies.
        if out:
            _write_atomic(out, result)
    return result or {"model": model, "runs": runs, **candidate_summary(cases, runs)}
