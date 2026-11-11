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


def score(case: Case, answer: Answer | None) -> CaseScore:
    # An unavailable answer (None) is wrong on every applicable field.
    change = case.change_shas.get(answer.suspected_change, NONE) if answer else None
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


def _percent(values: list[bool | None]) -> float:
    applicable = [v for v in values if v is not None]
    if not applicable:
        return 0.0
    return round(100 * sum(applicable) / len(applicable), 1)


def summarize(scores: list[CaseScore]) -> Metrics:
    return Metrics(
        change_hit=_percent([s.change_hit for s in scores]),
        runbook_hit=_percent([s.runbook_hit for s in scores]),
        none_change=_percent([s.none_change_ok for s in scores]),
        none_runbook=_percent([s.none_runbook_ok for s in scores]),
    )


def rule_accuracy(cases: list[Case]) -> float:
    return _percent([c.context.rule.category == c.expected.category for c in cases])
