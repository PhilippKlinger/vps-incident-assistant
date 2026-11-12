import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from assistant.context import IncidentContext
from assistant.models import Analysis

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = Path(__file__).parent / "prompts" / "system.md"
ANCHOR_CHARS = 500
FILES_IN_PROMPT = 10
FILE_NAME_MAX = 120
# Shared by the file lists of all changes, next to the diff budget in context.py.
FILES_BUDGET = 2000
_WINDOW_SECONDS = 3600
TOOL_NAME = "record_analysis"
MAX_OUTPUT_TOKENS = 1024


class AnalysisRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _BadResponse(Exception):
    """The model API answered, but not in the expected shape."""


@dataclass(frozen=True)
class ModelReply:
    text: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Unavailable:
    reason: str


def analysis_schema(ctx: IncidentContext) -> dict:
    # The forced tool gets this schema, so the ids of this incident are fixed here;
    # validate_analysis still checks them.
    schema = Analysis.model_json_schema()
    props = schema["properties"]
    props["suspected_change"]["enum"] = [c.id for c in ctx.changes] + ["none"]
    props["runbook"]["enum"] = [a.id for a in ctx.anchors] + ["none"]
    props["evidence"]["items"]["enum"] = [f.id for f in ctx.facts] + [c.id for c in ctx.changes]
    return schema


def _untrusted(text: str, nonce: str) -> str:
    # Without "=====" the text cannot forge or close a marker line.
    clean = text.replace("=====", "")
    return f"=====BEGIN UNTRUSTED {nonce}=====\n{clean}\n=====END UNTRUSTED {nonce}====="


def _short(name: str) -> str:
    return name if len(name) <= FILE_NAME_MAX else "..." + name[-(FILE_NAME_MAX - 3) :]


def _files_line(files: tuple[str, ...], remaining: int) -> str:
    if not files:
        return "Files: none"
    shown = ", ".join(_short(name) for name in files[:FILES_IN_PROMPT])
    if len(shown) > remaining:
        return f"Files: {len(files)} files (list omitted)"
    line = f"Files: {shown}"
    if len(files) > FILES_IN_PROMPT:
        line += f" (+{len(files) - FILES_IN_PROMPT} more)"
    return line


def build_messages(ctx: IncidentContext, nonce: str) -> list[dict]:
    system = _SYSTEM_PROMPT.read_text(encoding="utf-8")
    rule = ctx.rule
    lines = [
        "## Rule result",
        f"Category: {rule.category}",
        f"Severity: {rule.severity}",
        f"Reason: {rule.reason}",
        "",
        "## Facts",
    ]
    for fact in ctx.facts:
        lines.append(f"{fact.id}: {_untrusted(fact.text, nonce)}")
    lines += ["", "## Changes"]
    files_left = FILES_BUDGET
    for change in ctx.changes:
        files = _files_line(change.files, files_left)
        files_left -= len(files)
        head = f"{change.id} · {change.repo} · {change.sha[:7]} · {change.merged_at.isoformat()}"
        text = f"{files}\n{change.title}\n{change.body}\n{change.diff_excerpt}"
        lines += [head, _untrusted(text, nonce)]
    lines += ["", "## Runbook sections"]
    for anchor in ctx.anchors:
        text = f"{anchor.title}\n{anchor.text[:ANCHOR_CHARS]}"
        lines += [anchor.id, _untrusted(text, nonce)]
    change_ids = [c.id for c in ctx.changes] + ["none"]
    anchor_ids = [a.id for a in ctx.anchors] + ["none"]
    evidence_ids = [f.id for f in ctx.facts] + [c.id for c in ctx.changes]
    lines += [
        "",
        "## Allowed IDs",
        f"suspected_change: {', '.join(change_ids)}",
        f"runbook: {', '.join(anchor_ids)}",
        f"evidence: {', '.join(evidence_ids)}",
    ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]


class BedrockClient:
    def __init__(
        self, client: httpx.Client, region: str, model: str, token: str, timeout: float = 60
    ):
        self._client = client
        self._url = (
            f"https://bedrock-runtime.{region}.amazonaws.com/model/{quote(model, safe='')}/converse"
        )
        self._token = token
        self._timeout = timeout

    def chat(self, messages: list[dict], schema: dict) -> ModelReply:
        system, user = messages
        response = self._client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "system": [{"text": system["content"]}],
                "messages": [{"role": "user", "content": [{"text": user["content"]}]}],
                "inferenceConfig": {"temperature": 0, "maxTokens": MAX_OUTPUT_TOKENS},
                "toolConfig": {
                    "tools": [
                        {
                            "toolSpec": {
                                "name": TOOL_NAME,
                                "description": "Record the analysis of the incident.",
                                "inputSchema": {"json": schema},
                            }
                        }
                    ],
                    "toolChoice": {"tool": {"name": TOOL_NAME}},
                },
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        try:
            data = response.json()
            if data["stopReason"] != "tool_use":
                raise _BadResponse
            tool_input = next(
                block["toolUse"]["input"]
                for block in data["output"]["message"]["content"]
                if block.get("toolUse", {}).get("name") == TOOL_NAME
            )
            input_tokens = data["usage"]["inputTokens"]
            output_tokens = data["usage"]["outputTokens"]
        except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as error:
            raise _BadResponse from error
        if not isinstance(tool_input, dict):
            raise _BadResponse
        if not all(type(n) is int for n in (input_tokens, output_tokens)):
            raise _BadResponse
        return ModelReply(json.dumps(tool_input), input_tokens, output_tokens)


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
    elif isinstance(error, httpx.HTTPStatusError):
        detail += f": {error.response.status_code}"  # status only, never body or URL
    log.warning("llm attempt failed: %s (%s)", reason, detail)
    return reason


def analyze(ctx: IncidentContext, llm: BedrockClient, attempts: int = 2) -> Analysis | Unavailable:
    # Built outside the error handling so a bug here is not reported as a model outage.
    messages = build_messages(ctx, secrets.token_hex(8))
    schema = analysis_schema(ctx)
    reason = "model unreachable"
    for _ in range(attempts):
        try:
            reply = llm.chat(messages, schema)
            return validate_analysis(reply.text, ctx)
        except httpx.TimeoutException as error:
            reason = _failed("model timed out", error)
        except (httpx.RequestError, httpx.HTTPStatusError, _BadResponse) as error:
            reason = _failed("model unreachable", error)
        except AnalysisRejected as error:
            reason = _failed("invalid response", error)
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
