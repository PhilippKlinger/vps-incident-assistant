import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from evals.cases import Expected
from evals.check import check_set
from tests.evals_support import FIXTURES, load_fixture_set

ROOT = Path(__file__).parent.parent


def test_group_counts():
    store, cases = load_fixture_set()

    problems = check_set(cases, store)

    assert "group app: 1 cases, expected 12" in problems
    assert "group platform: 0 cases, expected 8" in problems
    assert "group none: 1 cases, expected 6" in problems
    assert "group injection: 0 cases, expected 4" in problems


def test_duplicate_id():
    store, cases = load_fixture_set()

    assert "duplicate case id: app-01-cors" in check_set([cases[0], cases[0]], store)


def test_expected_sha_not_among_changes():
    store, cases = load_fixture_set()
    bad = replace(cases[0], expected=replace(cases[0].expected, suspected_change="f" * 40))

    assert "app-01-cors: expected change is not among the changes" in check_set([bad], store)


def test_expected_anchor_not_among_frozen_anchors():
    store, cases = load_fixture_set()
    bad = replace(cases[0], expected=replace(cases[0].expected, runbook="vps-edge#nope"))

    assert "app-01-cors: expected runbook vps-edge#nope is not a frozen anchor" in check_set(
        [bad], store
    )


def test_injection_case_without_canary():
    store, cases = load_fixture_set()
    bad = replace(cases[0], group="injection", canary=None)

    assert "app-01-cors: injection case has no canary" in check_set([bad], store)


def test_history_count_outside_range():
    store, cases = load_fixture_set()

    # Two harmless commits besides the cause: 2 < 3.
    assert "app-01-cors: 2 history commits, expected 3-8" in check_set(cases, store)


def test_recency_share_outside_range():
    store, cases = load_fixture_set()

    # The only cause is the newest change: 100 % > 50 %.
    assert "cause is the newest change in 100 % of cases, expected 25-50 %" in check_set(
        cases, store
    )


def test_too_few_runbook_none_cases():
    store, cases = load_fixture_set()

    problems = check_set(cases, store)

    assert "0 cases with a change but no runbook, expected at least 3" in problems


def _variant(base, number, group, runbook, cause_newest):
    # Three harmless commits around the cause, built from the fixture case's own changes.
    harmless = [
        base.context.changes[1].model_copy(update={"sha": f"{i:040x}"}) for i in range(1, 4)
    ]
    cause = base.context.changes[0]
    order = [cause, *harmless] if cause_newest else [harmless[0], cause, *harmless[1:]]
    changes = tuple(c.model_copy(update={"id": f"C{n}"}) for n, c in enumerate(order, 1))
    return replace(
        base,
        id=f"case-{number}",
        group=group,
        canary="CANARY" if group == "injection" else None,
        context=replace(base.context, changes=changes),
        change_shas={c.id: c.sha for c in changes},
        expected=Expected("upstream_502", cause.sha, runbook),
    )


def test_valid_set_has_no_problems():
    store, cases = load_fixture_set()
    groups = ["app"] * 12 + ["platform"] * 8 + ["none"] * 6 + ["injection"] * 4
    pool = [
        _variant(
            cases[0],
            n,
            group,
            "none" if n % 4 == 0 else "vps-edge#upstream-502",
            cause_newest=n % 3 == 0,
        )
        for n, group in enumerate(groups)
    ]

    assert check_set(pool, store) == []


def test_cli_check_reports_missing_cases_in_one_line(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-m", "evals", "check", "--cases", str(tmp_path / "none")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "Traceback" not in output
    assert len(output.strip().splitlines()) == 1


def test_cli_baselines_prints_table():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals",
            "baselines",
            "--cases",
            str(FIXTURES / "cases"),
            "--data",
            str(FIXTURES),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "latest_change" in result.stdout
    assert "keyword_match" in result.stdout
    assert "rule accuracy" in result.stdout.lower()


def test_recency_share_is_compared_unrounded():
    store, cases = load_fixture_set()
    base = cases[0]
    second = base.change_shas["C2"]
    newest = base
    older = replace(base, expected=replace(base.expected, suspected_change=second))
    # 16 of 65 = 24.6 %: rounding to 25 would let it pass.
    pool = [replace(newest, id=f"n{i}") for i in range(16)]
    pool += [replace(older, id=f"o{i}") for i in range(49)]

    problems = check_set(pool, store)

    assert "cause is the newest change in 24.6 % of cases, expected 25-50 %" in problems


def test_canary_must_survive_sanitizing_unchanged():
    store, cases = load_fixture_set()
    case = replace(cases[0], group="injection", canary="BAD_CANARY")

    assert "app-01-cors: canary must match [A-Za-z0-9-]+" in check_set([case], store)
