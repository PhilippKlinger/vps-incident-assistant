import re
from collections import Counter

from assistant.runbook import parse_runbook
from evals.cases import Case, HistoryStore

GROUPS = {"app": 12, "platform": 8, "none": 6, "injection": 4}
HISTORY_RANGE = (3, 8)
RECENCY_RANGE_PCT = (25, 50)
MIN_RUNBOOK_NONE = 3
NONE = "none"
CANARY = re.compile(r"[A-Za-z0-9-]+")


def _anchor_ids(case: Case, store: HistoryStore) -> set[str]:
    return {
        anchor.id
        for repo in case.context.project.runbook_repos
        for anchor in parse_runbook(repo, store.runbook(repo))
    }


def _case_problems(case: Case, store: HistoryStore) -> list[str]:
    problems = []
    expected = case.expected
    shas = set(case.change_shas.values())
    has_cause = expected.suspected_change != NONE
    if has_cause and expected.suspected_change not in shas:
        problems.append(f"{case.id}: expected change is not among the changes")
    if expected.runbook != NONE and expected.runbook not in _anchor_ids(case, store):
        problems.append(f"{case.id}: expected runbook {expected.runbook} is not a frozen anchor")
    if case.group == "injection" and not case.canary:
        problems.append(f"{case.id}: injection case has no canary")
    # Sanitizing escapes other characters, which would hide a leak from the comment check.
    if case.canary and not CANARY.fullmatch(case.canary):
        problems.append(f"{case.id}: canary must match [A-Za-z0-9-]+")
    history = len(shas) - (1 if has_cause and expected.suspected_change in shas else 0)
    low, high = HISTORY_RANGE
    if not low <= history <= high:
        problems.append(f"{case.id}: {history} history commits, expected {low}-{high}")
    return problems


def check_set(cases: list[Case], store: HistoryStore) -> list[str]:
    problems = []
    counts = Counter(case.group for case in cases)
    for group, expected in GROUPS.items():
        if counts[group] != expected:
            problems.append(f"group {group}: {counts[group]} cases, expected {expected}")
    for group in counts.keys() - GROUPS.keys():
        problems.append(f"unknown group: {group}")
    for case_id, n in Counter(case.id for case in cases).items():
        if n > 1:
            problems.append(f"duplicate case id: {case_id}")
    for case in cases:
        problems += _case_problems(case, store)

    # Share of cases where the newest change is the cause: keeps latest_change from being
    # trivially right or useless.
    with_cause = [c for c in cases if c.expected.suspected_change != NONE]
    if with_cause:
        newest = sum(
            1 for c in with_cause if c.change_shas.get("C1") == c.expected.suspected_change
        )
        share = 100 * newest / len(with_cause)
        low, high = RECENCY_RANGE_PCT
        if not low <= share <= high:
            problems.append(
                f"cause is the newest change in {round(share, 1):g} % of cases, "
                f"expected {low}-{high} %"
            )
    no_runbook = sum(1 for c in with_cause if c.expected.runbook == NONE)
    if no_runbook < MIN_RUNBOOK_NONE:
        problems.append(
            f"{no_runbook} cases with a change but no runbook, expected at least {MIN_RUNBOOK_NONE}"
        )
    return problems
