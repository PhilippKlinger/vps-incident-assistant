import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from assistant.models import KumaEvent

INCIDENT = "incident"
RESOLVED = "resolved-pending-review"
MONITOR_PREFIX = "monitor:"
# GitHub limits label names to 50 characters.
_LABEL_MAX = 50


class Action(StrEnum):
    CREATE = "create"
    APPEND = "append"
    REOPEN_FLAP = "reopen_flap"
    MONITOR_UP = "monitor_up"
    RESOLVE = "resolve"
    IGNORE = "ignore"


@dataclass(frozen=True)
class Issue:
    number: int
    labels: frozenset[str]
    created_at: datetime
    updated_at: datetime


def project_label(name: str) -> str:
    return f"project:{name}"


def monitor_label(monitor_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", monitor_name.lower()).strip("-")
    return (MONITOR_PREFIX + slug)[:_LABEL_MAX].rstrip("-")


def decide(
    event: KumaEvent,
    issue: Issue | None,
    monitor_label: str,
    now: datetime,
    window: timedelta,
) -> Action:
    if event.status == "down":
        return _decide_down(issue, monitor_label, now, window)
    return _decide_up(issue, monitor_label)


def _decide_down(issue: Issue | None, label: str, now: datetime, window: timedelta) -> Action:
    if issue is None:
        return Action.CREATE
    if RESOLVED in issue.labels:
        # A quick flap goes back into the same issue; later it is a new incident.
        return Action.REOPEN_FLAP if now - issue.updated_at <= window else Action.CREATE
    # Kuma may deliver the same down event twice. Later downs of that monitor are added,
    # so a lost "up" does not silence the monitor until a human closes the issue.
    duplicate = label in issue.labels and now - issue.updated_at <= window
    return Action.IGNORE if duplicate else Action.APPEND


def _decide_up(issue: Issue | None, label: str) -> Action:
    if issue is None or label not in issue.labels:
        return Action.IGNORE
    others = {x for x in issue.labels if x.startswith(MONITOR_PREFIX)} - {label}
    return Action.MONITOR_UP if others else Action.RESOLVE
