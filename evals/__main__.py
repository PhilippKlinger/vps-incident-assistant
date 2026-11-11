import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

import httpx

from assistant.config import load_config
from assistant.github_read import GitHubReader, GitHubUnavailable, make_client
from assistant.llm import OllamaClient
from evals.baselines import keyword_match, latest_change
from evals.cases import Case, HistoryStore, load_cases
from evals.check import check_set
from evals.freeze import freeze
from evals.meter import DockerMeter, NullMeter
from evals.report import build_report, render_markdown
from evals.runner import PRODUCTION_TIMEOUT_S, describe_run, fetch_provenance, run_candidate
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


def _client() -> httpx.Client:
    return httpx.Client()


def _run_problem(runs: list[dict]) -> str | None:
    # Timeouts and invalid answers are model behaviour and show up in the report;
    # the rest points at the harness or the setup.
    if any(run["unsupported"] for run in runs):
        return "unsupported statement in a published comment"
    if any(run["unavailable"] == "model unreachable" for run in runs):
        return "model unreachable"
    if all(run["unavailable"] for run in runs):
        return "no case produced an answer"
    return None


def _run(args: argparse.Namespace) -> int:
    if not args.model or not args.out:
        raise SystemExit("run needs --model and --out")
    _, cases = _load(args)
    if args.only:
        wanted = args.only.split(",")
        unknown = sorted(set(wanted) - {case.id for case in cases})
        if unknown:
            raise SystemExit(f"Unknown case ids: {', '.join(unknown)}")
        cases = [case for case in cases if case.id in wanted]
    client = _client()
    ollama = OllamaClient(client, args.ollama_url, args.model, args.timeout)
    provenance = fetch_provenance(client, args.ollama_url, args.model)
    meter = DockerMeter(args.container) if args.container else NullMeter()
    result = run_candidate(
        cases,
        ollama,
        meter,
        args.model,
        out=args.out,
        on_case=lambda r: print(describe_run(r)),
        provenance=provenance,
    )
    print(f"{args.model}: {len(cases)} cases, results in {args.out}")
    problem = _run_problem(result["runs"])
    if problem:
        print(f"FAILED: {problem}", file=sys.stderr)
    return 1 if problem else 0


def _latency_runs(specs: list[str]) -> dict[str, dict]:
    found = {}
    for spec in specs:
        model, sep, path = spec.rpartition("=")
        if not sep or not model or not path:
            raise SystemExit(f"--latency-run expects MODEL=FILE, got: {spec}")
        try:
            found[model] = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Cannot read latency run {path}: {type(exc).__name__}") from None
    return found


def _report(args: argparse.Namespace) -> int:
    if not args.runs or not args.out_dir:
        raise SystemExit("report needs --runs and --out-dir")
    _, cases = _load(args)
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in args.runs]
    report = build_report(cases, runs, _latency_runs(args.latency_run))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(report["verdict"])
    return 0


def _candidates(args: argparse.Namespace) -> int:
    table = tomllib.loads((HERE / "candidates.toml").read_text(encoding="utf-8"))
    matrix = [{"model": c["model"], "smoke": c.get("smoke", False)} for c in table["candidates"]]
    smoke = next((c["model"] for c in matrix if c["smoke"]), None)
    print(json.dumps({"matrix": matrix, "smoke_model": smoke, "smoke_cases": table["smoke_cases"]}))
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
    parser.add_argument(
        "command", choices=["check", "baselines", "freeze", "run", "report", "candidates"]
    )
    parser.add_argument("--cases", type=Path, default=HERE / "cases")
    parser.add_argument("--data", type=Path, default=HERE)
    parser.add_argument("--config", type=Path, default=HERE.parent / "config.toml")
    parser.add_argument("--model")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--container")
    parser.add_argument("--only")
    parser.add_argument("--timeout", type=float, default=PRODUCTION_TIMEOUT_S)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--runs", type=Path, nargs="+")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--latency-run", action="append", default=[], metavar="MODEL=FILE")
    args = parser.parse_args(argv)
    commands = {
        "check": _check,
        "baselines": _baselines,
        "freeze": _freeze,
        "run": _run,
        "report": _report,
        "candidates": _candidates,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
