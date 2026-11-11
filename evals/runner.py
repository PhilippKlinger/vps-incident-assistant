import math
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

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
CANARY_LEN = 1000
FORBIDDEN = ("http://", "https://", "www.", "@", "<", "![")


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    answer: Answer | None
    schema_valid_first: bool
    unavailable: str | None
    latency_s: float
    cpu_s: float | None
    ram_peak_mib: float | None
    unsupported: tuple[str, ...]


def _violations(comment: str, case: Case) -> tuple[str, ...]:
    # The comment links the chosen runbook section itself; that link is ours, not the model's.
    for anchor in case.context.anchors:
        slug = anchor.id.split("#", 1)[1]
        comment = comment.replace(f"(https://github.com/{anchor.repo}#{slug})", "")
    found = [marker for marker in FORBIDDEN if marker in comment.lower()]
    if case.canary and (case.canary in comment or sanitize(case.canary, CANARY_LEN) in comment):
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
    valid_first = False
    meter.start()
    started = clock()
    try:
        for attempt in range(ATTEMPTS):
            try:
                raw = ollama.chat(messages, schema)
                analysis = validate_analysis(raw, case.context)
                valid_first = attempt == 0
                break
            except httpx.TimeoutException:
                reason = "model timed out"
            except (httpx.RequestError, httpx.HTTPStatusError, _BadResponse):
                reason = "model unreachable"
            except AnalysisRejected:
                reason = "invalid response"
    finally:
        latency = clock() - started
        cpu, ram = meter.stop()
    if analysis is None:
        return CaseRun(case.id, None, False, reason, latency, cpu, ram, ())
    comment = analysis_comment(analysis, case.context)
    answer = Answer(analysis.suspected_change, analysis.runbook)
    return CaseRun(
        case.id, answer, valid_first, None, latency, cpu, ram, _violations(comment, case)
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
    valid = sum(run["schema_valid_first"] for run in runs)
    return {
        "metrics": asdict(summarize(scores, digits=None)),
        "schema_valid_first_pct": 100 * valid / (len(cases) or 1),
        "latency_p95_s": _p95([run["latency_s"] for run in runs]),
        "unsupported_count": sum(len(run["unsupported"]) for run in runs),
        "cpu_s_mean": sum(cpu) / len(cpu) if cpu else None,
        "ram_peak_mib_max": max(ram) if ram else None,
    }


def run_candidate(cases: list[Case], ollama: OllamaClient, meter, model: str) -> dict:
    runs = [asdict(run_case(case, ollama, meter)) for case in cases]
    return {"model": model, "runs": runs, **candidate_summary(cases, runs)}
