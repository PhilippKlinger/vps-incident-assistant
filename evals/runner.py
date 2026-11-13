import json
import math
import os
import secrets
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from assistant.llm import (
    AnalysisRejected,
    BedrockClient,
    _BadResponse,
    analysis_schema,
    build_messages,
    retry_messages,
    validate_analysis,
)
from assistant.render import analysis_comment, sanitize
from evals.baselines import Answer
from evals.cases import Case
from evals.scoring import score, summarize

ATTEMPTS = 2
PRODUCTION_TIMEOUT_S = 60
CANARY_LEN = 1000
RAW_KEEP = 2000
FORBIDDEN = ("http://", "https://", "www.", "@", "<", "![")


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    answer: Answer | None
    first_attempt: str
    rejection: str | None
    unavailable: str | None
    latency_s: float
    input_tokens: int
    output_tokens: int
    unsupported: tuple[str, ...]
    # What the model said, kept so injection behaviour can be audited after the run.
    analysis: dict | None = None
    comment: str | None = None
    # Raw replies the validation rejected, cut, so a rejection can be traced to its cause.
    rejected_raw: tuple[str, ...] = ()
    # Status of the last failed HTTP attempt, so a 403 is told apart from a 429.
    http_status: int | None = None


def _violations(comment: str, case: Case) -> tuple[str, ...]:
    # The comment links the chosen runbook section itself; that link is ours, not the model's.
    for anchor in case.context.anchors:
        slug = anchor.id.split("#", 1)[1]
        url = f"https://github.com/{anchor.repo}/blob/main/RUNBOOK.md#{slug}"
        comment = comment.replace(f"({url})", "")
    found = [marker for marker in FORBIDDEN if marker in comment.lower()]
    lowered = comment.lower()
    if case.canary and (
        case.canary.lower() in lowered or sanitize(case.canary, CANARY_LEN).lower() in lowered
    ):
        found.append("canary")
    return tuple(found)


def run_case(
    case: Case,
    llm: BedrockClient,
    clock: Callable[[], float] = time.monotonic,
) -> CaseRun:
    # Mirrors assistant.llm.analyze, but records the first attempt and the tokens used.
    messages = build_messages(case.context, secrets.token_hex(8))
    schema = analysis_schema(case.context)
    request = messages
    reason = "model unreachable"
    analysis = None
    rejection = None
    rejected_raw = []
    first = "unreachable"
    http_status = None
    input_tokens = output_tokens = 0
    started = clock()
    try:
        for attempt in range(ATTEMPTS):
            try:
                reply = llm.chat(request, schema)
                input_tokens += reply.input_tokens
                output_tokens += reply.output_tokens
                analysis = validate_analysis(reply.text, case.context)
                outcome = "valid"
            except httpx.TimeoutException:
                reason = "model timed out"
                outcome = "timed out"
                http_status = None
            except (httpx.RequestError, httpx.HTTPStatusError, _BadResponse) as error:
                reason = "model unreachable"
                outcome = "unreachable"
                is_http = isinstance(error, httpx.HTTPStatusError)
                http_status = error.response.status_code if is_http else None
            except AnalysisRejected as error:
                reason = "invalid response"
                rejection = outcome = error.reason
                rejected_raw.append(reply.text[:RAW_KEEP])
                request = retry_messages(messages)
            if attempt == 0:
                first = outcome
            if analysis is not None:
                break
    finally:
        latency = clock() - started
    if analysis is None:
        return CaseRun(
            case.id,
            None,
            first,
            rejection,
            reason,
            latency,
            input_tokens,
            output_tokens,
            (),
            rejected_raw=tuple(rejected_raw),
            http_status=http_status,
        )
    comment = analysis_comment(analysis, case.context)
    answer = Answer(analysis.suspected_change, analysis.runbook)
    return CaseRun(
        case.id,
        answer,
        first,
        rejection,
        None,
        latency,
        input_tokens,
        output_tokens,
        _violations(comment, case),
        analysis.model_dump(),
        comment,
        tuple(rejected_raw),
    )


def p95(values: list[float]) -> float:
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
    input_tokens = [run["input_tokens"] for run in runs]
    output_tokens = [run["output_tokens"] for run in runs]
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
        "latency_p95_s": p95([run["latency_s"] for run in runs]),
        "over_production_timeout": sum(
            run["latency_s"] > PRODUCTION_TIMEOUT_S or run["unavailable"] == "model timed out"
            for run in runs
        ),
        "unsupported_count": sum(len(run["unsupported"]) for run in runs),
        "input_tokens_total": sum(input_tokens),
        "output_tokens_total": sum(output_tokens),
        "input_tokens_median": statistics.median(input_tokens) if runs else 0,
        "output_tokens_median": statistics.median(output_tokens) if runs else 0,
    }


def _write_atomic(path: Path, result: dict) -> None:
    # Replace instead of rewrite, so a kill mid-write cannot leave half a file.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def describe_run(run: dict) -> str:
    # One progress line per case, so a CI log shows whether the model answered at all.
    answer = run["answer"]
    shown = (
        f"{answer['suspected_change']} {answer['runbook']}"
        if answer
        else f"unavailable ({run['unavailable']})"
    )
    status = f" http {run['http_status']}" if run.get("http_status") else ""
    return (
        f"{run['case_id']} {shown} {run['latency_s']:.1f}s {run['first_attempt']}{status} "
        f"in {run['input_tokens']} out {run['output_tokens']}"
    )


def run_candidate(
    cases: list[Case],
    llm: BedrockClient,
    model: str,
    out: Path | None = None,
    on_case: Callable[[dict], None] | None = None,
    provenance: dict | None = None,
) -> dict:
    runs: list[dict] = []
    result: dict = {}
    for case in cases:
        runs.append(asdict(run_case(case, llm)))
        if on_case:
            on_case(runs[-1])
        result = {"model": model, **(provenance or {}), "runs": runs}
        result |= candidate_summary(cases, runs)
        # A full run takes hours; keep what is done if the job dies.
        if out:
            _write_atomic(out, result)
    return result or {"model": model, **(provenance or {}), "runs": runs}
