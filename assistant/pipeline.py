import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from assistant.config import Config, Settings
from assistant.context import IncidentContext, build_context
from assistant.github_read import GitHubReader
from assistant.issues import IssueClient, IssueUnavailable
from assistant.lifecycle import (
    INCIDENT,
    RESOLVED,
    Action,
    Issue,
    decide,
    monitor_label,
    project_label,
)
from assistant.llm import OllamaClient, RateLimiter, Unavailable, analyze
from assistant.models import Analysis, KumaEvent
from assistant.probe import ProbeResult
from assistant.render import (
    analysis_comment,
    append_comment,
    flap_comment,
    issue_body,
    issue_title,
    monitor_up_comment,
    resolved_comment,
    telegram_text,
    unavailable_comment,
)
from assistant.rules import classify
from assistant.telegram import TelegramError

log = logging.getLogger(__name__)

# Kuma sends the Web-App and API alarms almost at once; without this lock both would
# find no open issue and create one each.
_ISSUE_LOCK = threading.Lock()


@dataclass
class Deps:
    config: Config
    settings: Settings
    probe: Callable[[str], ProbeResult]
    reader: GitHubReader
    issues: IssueClient
    ollama: OllamaClient
    telegram: Callable[[str], None]
    limiter: RateLimiter
    now: Callable[[], datetime]


def _build_context(event: KumaEvent, project, deps: Deps, now: datetime) -> IncidentContext:
    probe = deps.probe(event.url) if event.url else None
    rule = classify(event, probe)
    reader = deps.reader if project.repos else None
    return build_context(event, project, probe, rule, reader, now)


def _create(ctx: IncidentContext, label: str, deps: Deps) -> tuple[int, bool]:
    """Create the issue; returns its number and whether an LLM run follows."""
    project = ctx.project
    cfg = deps.config
    # Order matters: the limiter is only consumed when a run will actually happen.
    run_llm = cfg.llm_enabled and project.llm and ctx.github_ok and deps.limiter.allow()
    issue = deps.issues.create(
        issue_title(ctx),
        issue_body(ctx, analysis_pending=run_llm),
        [INCIDENT, project_label(project.name), label],
    )
    return issue.number, run_llm


@dataclass(frozen=True)
class _FollowUp:
    ctx: IncidentContext
    number: int
    run_llm: bool


def _apply(
    action: Action, event: KumaEvent, issue: Issue | None, deps: Deps, now: datetime
) -> _FollowUp | None:
    """Write the issue changes for one action; the caller holds the lock."""
    project = deps.config.project_for(event.monitor_name)
    label = monitor_label(event.monitor_name)
    issues = deps.issues
    number = issue.number if issue else 0
    if action is Action.MONITOR_UP:
        issues.remove_label(number, label)
        issues.comment(number, monitor_up_comment(event.monitor_name))
    elif action is Action.RESOLVE:
        issues.remove_label(number, label)
        issues.comment(number, resolved_comment(now - issue.created_at))
        issues.add_labels(number, [RESOLVED])
    elif action is Action.APPEND:
        issues.comment(number, append_comment(_build_context(event, project, deps, now)))
        issues.add_labels(number, [label])
    elif action is Action.REOPEN_FLAP:
        issues.comment(number, flap_comment(_build_context(event, project, deps, now)))
        issues.remove_label(number, RESOLVED)
        issues.add_labels(number, [label])
    elif action is Action.CREATE:
        ctx = _build_context(event, project, deps, now)
        number, run_llm = _create(ctx, label, deps)
        return _FollowUp(ctx, number, run_llm)
    return None


def _log_step(action: Action, number: int | None, started: float, reason: str = "") -> None:
    ms = round((time.monotonic() - started) * 1000)
    log.info("action=%s issue=%s duration_ms=%d%s", action, number, ms, reason)


def _notify(deps: Deps, text: str) -> None:
    try:
        deps.telegram(text)
    except TelegramError as error:
        log.warning("telegram failed: %s", error)


def _follow_up(job: _FollowUp, deps: Deps) -> None:
    """LLM analysis, its comment and Telegram; runs after the lock is released."""
    ctx, number = job.ctx, job.number
    started = time.monotonic()
    url = f"https://github.com/{deps.settings.issue_repo}/issues/{number}"
    analysis: Analysis | None = None
    reason = ""
    if job.run_llm:
        result = analyze(ctx, deps.ollama)
        if isinstance(result, Unavailable):
            reason = f" unavailable={result.reason}"
            deps.issues.comment(number, unavailable_comment(result.reason))
        else:
            analysis = result
            deps.issues.comment(number, analysis_comment(result, ctx))
        _log_step(Action.CREATE, number, started, f" llm=done{reason}")
    _notify(deps, telegram_text(ctx, url, analysis))


def handle_event(event: KumaEvent, deps: Deps) -> None:
    started = time.monotonic()
    action = None
    try:
        now = deps.now()
        config = deps.config
        project = config.project_for(event.monitor_name)
        with _ISSUE_LOCK:
            issue = deps.issues.find_open(project.name)
            window = timedelta(minutes=config.merge_window_minutes)
            action = decide(event, issue, monitor_label(event.monitor_name), now, window)
            follow_up = (
                _apply(action, event, issue, deps, now) if action is not Action.IGNORE else None
            )
        number = follow_up.number if follow_up else (issue.number if issue else None)
        _log_step(action, number, started)
        if follow_up:
            _follow_up(follow_up, deps)
    except IssueUnavailable as error:
        log.error("%s during action=%s", type(error).__name__, action)


def run_event(event: KumaEvent, deps: Deps) -> None:
    try:
        handle_event(event, deps)
    except Exception as error:
        # Only the exception type goes into the message: no tokens, no prompt content.
        log.exception("unexpected error while handling event: %s", type(error).__name__)
