import argparse
import os
import sys
from pathlib import Path

from assistant.config import load_config
from assistant.github_read import GitHubReader, GitHubUnavailable, make_client
from evals.baselines import keyword_match, latest_change
from evals.cases import Case, HistoryStore, load_cases
from evals.check import check_set
from evals.freeze import freeze
from evals.scoring import rule_accuracy, score, summarize

HERE = Path(__file__).parent


def _load(args: argparse.Namespace) -> tuple[HistoryStore, list[Case]]:
    if not args.cases.is_dir() or not any(args.cases.glob("*.json")):
        raise SystemExit(f"No eval cases found in {args.cases}")
    try:
        store = HistoryStore.load(args.data)
        return store, load_cases(args.cases, store, load_config(args.config))
    except (KeyError, ValueError, OSError) as exc:
        # json.JSONDecodeError is a ValueError; keep the failure to one line.
        message = f"Cannot load eval cases: {type(exc).__name__}: {exc}"
        raise SystemExit(message.splitlines()[0]) from None


def _check(args: argparse.Namespace) -> int:
    store, cases = _load(args)
    problems = check_set(cases, store)
    print("\n".join(problems) if problems else f"OK: {len(cases)} cases")
    return 1 if problems else 0


def _baselines(args: argparse.Namespace) -> int:
    _, cases = _load(args)
    print(f"{'baseline':<14} {'change':>7} {'runbook':>8} {'none chg':>9} {'none rb':>8}")
    for name, baseline in (("latest_change", latest_change), ("keyword_match", keyword_match)):
        m = summarize([score(case, baseline(case)) for case in cases])
        print(
            f"{name:<14} {m.change_hit:>6.1f}% {m.runbook_hit:>7.1f}% "
            f"{m.none_change:>8.1f}% {m.none_runbook:>7.1f}%"
        )
    print(f"Rule accuracy: {rule_accuracy(cases):.1f}%")
    return 0


def _freeze(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required to freeze eval data")
    project = load_config(args.config).project_for("Videoflix · Web-App")
    try:
        freeze(GitHubReader(make_client(token)), project.repos, project.runbook_repos, args.data)
    except GitHubUnavailable as exc:
        raise SystemExit(f"Freeze failed: {exc}") from None
    print(f"Frozen {len(project.repos)} repos into {args.data}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("command", choices=["check", "baselines", "freeze"])
    parser.add_argument("--cases", type=Path, default=HERE / "cases")
    parser.add_argument("--data", type=Path, default=HERE)
    parser.add_argument("--config", type=Path, default=HERE.parent / "config.toml")
    args = parser.parse_args(argv)
    return {"check": _check, "baselines": _baselines, "freeze": _freeze}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
