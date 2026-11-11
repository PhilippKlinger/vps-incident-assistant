from dataclasses import dataclass

from evals.baselines import Answer
from evals.cases import Case

NONE = "none"


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    group: str
    # None = not applicable for this case.
    change_hit: bool | None
    runbook_hit: bool | None
    none_change_ok: bool | None
    none_runbook_ok: bool | None


@dataclass(frozen=True)
class Metrics:
    change_hit: float
    runbook_hit: float
    none_change: float
    none_runbook: float


def _resolve(case: Case, change_id: str) -> str | None:
    # An id the model invented resolves to nothing, so it can never count as a correct "none".
    return NONE if change_id == NONE else case.change_shas.get(change_id)


def score(case: Case, answer: Answer | None) -> CaseScore:
    # An unavailable answer (None) is wrong on every applicable field.
    change = _resolve(case, answer.suspected_change) if answer else None
    runbook = answer.runbook if answer else None
    expected = case.expected
    expects_change = expected.suspected_change != NONE
    expects_runbook = expected.runbook != NONE
    return CaseScore(
        case_id=case.id,
        group=case.group,
        change_hit=(change == expected.suspected_change) if expects_change else None,
        runbook_hit=(runbook == expected.runbook) if expects_runbook else None,
        none_change_ok=None if expects_change else (answer is not None and change == NONE),
        none_runbook_ok=None if expects_runbook else (answer is not None and runbook == NONE),
    )


def _percent(values: list[bool | None], digits: int | None = 1) -> float:
    applicable = [v for v in values if v is not None]
    if not applicable:
        return 0.0
    value = 100 * sum(applicable) / len(applicable)
    return value if digits is None else round(value, digits)


def summarize(scores: list[CaseScore], digits: int | None = 1) -> Metrics:
    # digits=None keeps the raw values, for comparing against thresholds.
    return Metrics(
        change_hit=_percent([s.change_hit for s in scores], digits),
        runbook_hit=_percent([s.runbook_hit for s in scores], digits),
        none_change=_percent([s.none_change_ok for s in scores], digits),
        none_runbook=_percent([s.none_runbook_ok for s in scores], digits),
    )


def rule_accuracy(cases: list[Case]) -> float:
    return _percent([c.context.rule.category == c.expected.category for c in cases])
