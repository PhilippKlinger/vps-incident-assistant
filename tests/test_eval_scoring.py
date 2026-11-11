from dataclasses import replace

from evals.baselines import Answer
from evals.cases import Expected
from evals.scoring import CaseScore, Metrics, rule_accuracy, score, summarize
from tests.evals_support import load_fixture_set


def test_hit():
    _, cases = load_fixture_set()

    result = score(cases[0], Answer("C1", "vps-edge#upstream-502"))

    assert result == CaseScore("app-01-cors", "app", True, True, None, None)


def test_miss():
    _, cases = load_fixture_set()

    result = score(cases[0], Answer("C2", "none"))

    assert (result.change_hit, result.runbook_hit) == (False, False)


def test_expected_none_answered_none():
    _, cases = load_fixture_set()

    result = score(cases[1], Answer("none", "none"))

    assert result == CaseScore("none-01-quiet", "none", None, None, True, True)


def test_expected_none_answered_with_a_change():
    _, cases = load_fixture_set()

    result = score(cases[1], Answer("C1", "vps-edge#proxy-down"))

    assert (result.none_change_ok, result.none_runbook_ok) == (False, False)


def test_unavailable_answer_is_wrong_on_every_applicable_field():
    _, cases = load_fixture_set()

    assert score(cases[0], None) == CaseScore("app-01-cors", "app", False, False, None, None)
    assert score(cases[1], None) == CaseScore("none-01-quiet", "none", None, None, False, False)


def test_summarize_percentages_with_one_decimal():
    scores = [
        CaseScore("a", "app", True, True, None, None),
        CaseScore("b", "app", True, False, None, None),
        CaseScore("c", "app", False, False, None, None),
        CaseScore("d", "none", None, None, True, False),
    ]

    assert summarize(scores) == Metrics(
        change_hit=66.7, runbook_hit=33.3, none_change=100.0, none_runbook=0.0
    )


def test_summarize_empty_denominator_is_zero():
    assert summarize([]) == Metrics(0.0, 0.0, 0.0, 0.0)


def test_rule_accuracy():
    _, cases = load_fixture_set()
    wrong = replace(cases[1], expected=Expected("tls", "none", "none"))

    assert rule_accuracy(cases) == 100.0
    assert rule_accuracy([cases[0], wrong]) == 50.0
