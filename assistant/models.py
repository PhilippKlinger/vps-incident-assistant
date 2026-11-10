from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Kuma heartbeat.status values we act on; pending (2) and maintenance (3) are ignored.
_STATUS = {0: "down", 1: "up"}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class KumaEvent(_Frozen):
    monitor_name: str
    url: str | None
    status: Literal["down", "up"]
    msg: str
    time: datetime


class Fact(_Frozen):
    id: str
    source: Literal["kuma", "probe", "github"]
    text: str


class Change(_Frozen):
    id: str
    repo: str
    sha: str
    # title, body and diff_excerpt come from GitHub and are untrusted input.
    title: str
    author: str
    merged_at: datetime
    files: tuple[str, ...]
    diff_excerpt: str
    body: str


class RunbookAnchor(_Frozen):
    id: str
    repo: str
    title: str
    text: str


def _parse_time(value: object) -> datetime | None:
    # Kuma sends UTC without offset ("2026-11-10 14:03:12.345"); ISO with offset is accepted too.
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_kuma(payload: dict) -> KumaEvent | None:
    """Turn a Kuma webhook body into an event; None for anything we should ignore."""
    if not isinstance(payload, dict):
        return None
    heartbeat = payload.get("heartbeat")
    monitor = payload.get("monitor")
    if not isinstance(heartbeat, dict) or not isinstance(monitor, dict):
        return None
    raw_status = heartbeat.get("status")
    status = _STATUS.get(raw_status) if isinstance(raw_status, int) else None
    name = monitor.get("name")
    time = _parse_time(heartbeat.get("time"))
    if status is None or not isinstance(name, str) or not name or time is None:
        return None
    url = monitor.get("url")
    msg = heartbeat.get("msg")
    return KumaEvent(
        monitor_name=name,
        url=url if isinstance(url, str) else None,
        status=status,
        msg=msg if isinstance(msg, str) else "",
        time=time,
    )
