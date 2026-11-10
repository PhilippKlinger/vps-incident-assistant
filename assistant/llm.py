import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError

from assistant.context import IncidentContext
from assistant.models import Analysis

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = Path(__file__).parent / "prompts" / "system.md"
ANCHOR_CHARS = 500
_WINDOW_SECONDS = 3600


class AnalysisRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _BadResponse(Exception):
    """Ollama answered, but not in the expected shape."""


@dataclass(frozen=True)
class Unavailable:
    reason: str


def analysis_schema() -> dict:
    return Analysis.model_json_schema()


def _untrusted(text: str, nonce: str) -> str:
    # Without "=====" the text cannot forge or close a marker line.
    clean = text.replace("=====", "")
    return f"=====BEGIN UNTRUSTED {nonce}=====\n{clean}\n=====END UNTRUSTED {nonce}====="


def build_messages(ctx: IncidentContext, nonce: str) -> list[dict]:
    system = _SYSTEM_PROMPT.read_text(encoding="utf-8")
    rule = ctx.rule
    lines = [
        "## Regel-Ergebnis",
        f"Kategorie: {rule.category}",
        f"Schweregrad: {rule.severity}",
        f"Grund: {rule.reason}",
        "",
        "## Fakten",
    ]
    for fact in ctx.facts:
        lines.append(f"{fact.id}: {_untrusted(fact.text, nonce)}")
    lines += ["", "## Änderungen"]
    for change in ctx.changes:
        head = f"{change.id} · {change.repo} · {change.sha[:7]} · {change.merged_at.isoformat()}"
        text = f"{change.title}\n{change.body}\n{change.diff_excerpt}"
        lines += [head, _untrusted(text, nonce)]
    lines += ["", "## Runbook-Abschnitte"]
    for anchor in ctx.anchors:
        text = f"{anchor.title}\n{anchor.text[:ANCHOR_CHARS]}"
        lines += [anchor.id, _untrusted(text, nonce)]
    change_ids = [c.id for c in ctx.changes] + ["none"]
    anchor_ids = [a.id for a in ctx.anchors] + ["none"]
    evidence_ids = [f.id for f in ctx.facts] + [c.id for c in ctx.changes]
    lines += [
        "",
        "## Erlaubte IDs",
        f"suspected_change: {', '.join(change_ids)}",
        f"runbook: {', '.join(anchor_ids)}",
        f"evidence: {', '.join(evidence_ids)}",
    ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]


class OllamaClient:
    def __init__(self, client: httpx.Client, url: str, model: str, timeout: float = 300):
        self._client = client
        self._url = url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def chat(self, messages: list[dict], schema: dict) -> str:
        response = self._client.post(
            f"{self._url}/api/chat",
            json={
                "model": self._model,
                "messages": messages,
                "stream": False,
                "think": False,
                "format": schema,
                "options": {"temperature": 0},
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        try:
            content = response.json()["message"]["content"]
        except (KeyError, TypeError, ValueError) as error:
            raise _BadResponse from error
        if not isinstance(content, str):
            raise _BadResponse
        return content


def validate_analysis(raw: str, ctx: IncidentContext) -> Analysis:
    try:
        analysis = Analysis.model_validate_json(raw)
    except ValidationError as error:
        raise AnalysisRejected("schema") from error
    change_ids = {c.id for c in ctx.changes}
    anchor_ids = {a.id for a in ctx.anchors}
    evidence_ids = {f.id for f in ctx.facts} | change_ids
    if analysis.suspected_change not in change_ids | {"none"}:
        raise AnalysisRejected("unknown_change")
    if analysis.runbook not in anchor_ids | {"none"}:
        raise AnalysisRejected("unknown_runbook")
    if any(item not in evidence_ids for item in analysis.evidence):
        raise AnalysisRejected("unknown_evidence")
    return analysis


def _failed(reason: str, error: Exception) -> str:
    detail = type(error).__name__
    if isinstance(error, AnalysisRejected):
        detail += f": {error.reason}"  # fixed English code, no model content
    log.warning("llm attempt failed: %s (%s)", reason, detail)
    return reason


def analyze(
    ctx: IncidentContext, ollama: OllamaClient, attempts: int = 2
) -> Analysis | Unavailable:
    # Built outside the error handling so a bug here is not reported as a model outage.
    messages = build_messages(ctx, secrets.token_hex(8))
    schema = analysis_schema()
    reason = "Modell nicht erreichbar"
    for _ in range(attempts):
        try:
            raw = ollama.chat(messages, schema)
            return validate_analysis(raw, ctx)
        except httpx.TimeoutException as error:
            reason = _failed("Zeitlimit erreicht", error)
        except (httpx.RequestError, httpx.HTTPStatusError, _BadResponse) as error:
            reason = _failed("Modell nicht erreichbar", error)
        except AnalysisRejected as error:
            reason = _failed("Antwort ungültig", error)
    return Unavailable(reason)


class RateLimiter:
    def __init__(self, max_per_hour: int, clock: Callable[[], float] = time.monotonic):
        self._max = max_per_hour
        self._clock = clock
        self._calls: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._calls and now - self._calls[0] > _WINDOW_SECONDS:
            self._calls.popleft()
        if len(self._calls) >= self._max:
            return False
        self._calls.append(now)
        return True
