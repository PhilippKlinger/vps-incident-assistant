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

LIMIT_REASON = "hourly limit reached"
INTERNAL_REASON = "internal error"

# Kuma sends the Web-App and API alarms almost at once; without this lock both would
# find no open issue and create one each.
_ISSUE_LOCK = threading.Lock()


# Ollama runs one request at a time; without this lock a second analysis waits in
# Ollama's queue and both time out.
_ANALYSIS_LOCK = threading.Lock()


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


def _safe_probe(event: KumaEvent, deps: Deps) -> ProbeResult | None:
    # The issue must be written even if the own check of the URL breaks unexpectedly.
    if not event.url:
        return None
    try:
        return deps.probe(event.url)
    except Exception as error:
        log.exception("probe failed: %s", type(error).__name__)
        return None


def _build_context(
    event: KumaEvent, project, deps: Deps, now: datetime, *, read_github: bool = True
) -> IncidentContext:
    probe = _safe_probe(event, deps)
    rule = classify(event, probe)
    reader = deps.reader if project.repos and read_github else None
    return build_context(event, project, probe, rule, reader, now)


def _create(ctx: IncidentContext, label: str, deps: Deps) -> "_FollowUp":
    project = ctx.project
    llm_wanted = deps.config.llm_enabled and project.llm and ctx.github_ok
    issue = deps.issues.create(
        issue_title(ctx),
        issue_body(ctx, analysis_pending=llm_wanted),
        [INCIDENT, project_label(project.name), label],
    )
    # The limiter slot is only taken once the issue exists and a run will follow.
    run_llm = llm_wanted and deps.limiter.allow()
    return _FollowUp(ctx, issue.number, run_llm, limited=llm_wanted and not run_llm)


@dataclass(frozen=True)
class _FollowUp:
    ctx: IncidentContext
    number: int
    run_llm: bool
    # The issue promised an analysis but the hourly limit refused it.
    limited: bool = False


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
        # Follow-up comments show only rule and facts; the GitHub read (about 60 requests)
        # would hold the issue lock for nothing.
        ctx = _build_context(event, project, deps, now, read_github=False)
        issues.comment(number, append_comment(ctx))
        issues.add_labels(number, [label])
    elif action is Action.REOPEN_FLAP:
        ctx = _build_context(event, project, deps, now, read_github=False)
        issues.comment(number, flap_comment(ctx))
        issues.remove_label(number, RESOLVED)
        issues.add_labels(number, [label])
    elif action is Action.CREATE:
        ctx = _build_context(event, project, deps, now)
        return _create(ctx, label, deps)
    return None


def _log_step(action: Action, number: int | None, started: float, reason: str = "") -> None:
    ms = round((time.monotonic() - started) * 1000)
    log.info("action=%s issue=%s duration_ms=%d%s", action, number, ms, reason)


def _notify(deps: Deps, text: str) -> None:
    try:
        deps.telegram(text)
    except TelegramError as error:
        log.warning("telegram failed: %s", error)


def _comment_unavailable(number: int, reason: str, deps: Deps) -> None:
    # Best effort: "Analysis pending" should not stay without an answer.
    try:
        deps.issues.comment(number, unavailable_comment(reason))
    except IssueUnavailable as error:
        log.error("%s during analysis comment", type(error).__name__)


def _comment_analysis(job: _FollowUp, deps: Deps) -> Analysis | None:
    """Run the LLM and post its comment; failures are logged and never block Telegram."""
    started = time.monotonic()
    analysis: Analysis | None = None
    reason = ""
    try:
        if job.limited:
            reason = LIMIT_REASON
            deps.issues.comment(job.number, unavailable_comment(reason))
        elif job.run_llm:
            with _ANALYSIS_LOCK:
                result = analyze(job.ctx, deps.ollama)
            if isinstance(result, Unavailable):
                reason = result.reason
                deps.issues.comment(job.number, unavailable_comment(reason))
            else:
                # Kept even if the comment fails below: Telegram can still carry it.
                analysis = result
                deps.issues.comment(job.number, analysis_comment(result, job.ctx))
    except IssueUnavailable as error:
        log.error("%s during analysis comment", type(error).__name__)
    except Exception as error:
        log.exception("analysis step failed: %s", type(error).__name__)
        reason = INTERNAL_REASON
        _comment_unavailable(job.number, reason, deps)
    if job.run_llm or job.limited:
        suffix = f" unavailable={reason}" if reason else ""
        _log_step(Action.CREATE, job.number, started, f" llm=done{suffix}")
    return analysis


def _follow_up(job: _FollowUp, deps: Deps) -> None:
    """LLM analysis, its comment and Telegram; runs after the lock is released."""
    url = f"https://github.com/{deps.settings.issue_repo}/issues/{job.number}"
    analysis = _comment_analysis(job, deps)
    _notify(deps, telegram_text(job.ctx, url, analysis))


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
            follow_up = _apply(action, event, issue, deps, now)
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
        # The message has only the type; the traceback still shows str(error), so error
        # messages in this code base must never carry tokens or prompt content.
        log.exception("unexpected error while handling event: %s", type(error).__name__)
