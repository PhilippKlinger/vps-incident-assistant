import json
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx

from assistant.config import Config, Project, Settings
from assistant.github_read import GitHubReader
from assistant.issues import IssueClient
from assistant.llm import OllamaClient, RateLimiter
from assistant.models import KumaEvent
from assistant.pipeline import Deps, handle_event, run_event
from assistant.probe import ProbeResult
from assistant.telegram import TelegramError

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
REPO = "owner/issues"
VIDEOFLIX = Project(
    "videoflix",
    "Videoflix",
    ("Videoflix · Web-App", "Videoflix · API"),
    ("o/vps-edge",),
    ("o/vps-edge",),
    True,
)
PORTFOLIO = Project("portfolio", "Portfolio", ("Portfolio",), (), (), False)
SETTINGS = Settings("s", "r", "i", REPO, "tok", "chat", "http://ollama:11434", "m")
PROBE = ProbeResult(502, None, 40, {}, 30, ("1.2.3.4",))
ANSWER = {
    "suspected_change": "none",
    "change_reasoning": "no change",
    "runbook": "none",
    "runbook_reasoning": "nothing matches",
    "summary": "The upstream does not respond.",
    "evidence": ["F1"],
    "confidence": "low",
}


def _event(name="Videoflix · API", status="down", url="https://x.example/health"):
    return KumaEvent(monitor_name=name, url=url, status=status, msg="boom", time=NOW)


class Store:
    """Fake GitHub issues API that remembers issues, so find_open sees created ones."""

    def __init__(self, delay: float = 0):
        self.issues: list[dict] = []
        self.requests: list[tuple[str, str, dict | None]] = []
        self.delay = delay
        self.lock = threading.Lock()

    def seed(self, number, labels, created_at=NOW - timedelta(minutes=5), updated_at=None):
        self.issues.append(
            {
                "number": number,
                "labels": [{"name": n} for n in labels],
                "created_at": created_at.isoformat(),
                "updated_at": (updated_at or created_at).isoformat(),
            }
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix(f"/repos/{REPO}")
        with self.lock:
            self.requests.append((request.method, path, body))
        if request.method == "GET":
            with self.lock:
                snapshot = self.issues[-1:]
            # Answer from the snapshot after a pause: without the pipeline lock the second
            # find_open sees no issue yet.
            time.sleep(self.delay)
            return httpx.Response(200, json=snapshot)
        if path == "/issues":
            with self.lock:
                issue = {
                    "number": len(self.issues) + 1,
                    "labels": [{"name": n} for n in body["labels"]],
                    "created_at": NOW.isoformat(),
                    "updated_at": NOW.isoformat(),
                }
                self.issues.append(issue)
            return httpx.Response(201, json=issue)
        number = int(path.split("/")[2])
        issue = next(i for i in self.issues if i["number"] == number)
        if path.endswith("/labels") and request.method == "POST":
            issue["labels"] += [{"name": n} for n in body["labels"]]
        elif request.method == "DELETE":
            name = path.rsplit("/", 1)[-1]
            issue["labels"] = [x for x in issue["labels"] if x["name"] != name]
        return httpx.Response(200, json={})

    def calls(self, method, suffix=""):
        return [b for m, p, b in self.requests if m == method and p.endswith(suffix)]


def _reader(fail=False) -> GitHubReader:
    def handler(request):
        if fail:
            return httpx.Response(500)
        path = request.url.path
        if path.endswith("/commits"):
            return httpx.Response(200, json=[])
        if path.endswith("/runs"):
            return httpx.Response(200, json={"workflow_runs": []})
        return httpx.Response(200, text="## Troubleshooting\n### 502\nCheck the upstream\n")

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubReader(client)


class Setup:
    def __init__(self, *, llm=True, ollama_status=200, github_down=False, limit=6, delay=0):
        self.store = Store(delay)
        self.ollama_calls = 0
        self.telegram: list[str] = []
        self.probe_calls: list[str] = []

        def ollama(request):
            self.ollama_calls += 1
            if ollama_status != 200:
                return httpx.Response(ollama_status)
            return httpx.Response(200, json={"message": {"content": json.dumps(ANSWER)}})

        issue_http = httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(self.store.handler)
        )
        ollama_http = httpx.Client(transport=httpx.MockTransport(ollama))
        self.deps = Deps(
            config=Config((VIDEOFLIX, PORTFOLIO), llm, 6, 10),
            settings=SETTINGS,
            probe=self._probe,
            reader=_reader(github_down),
            issues=IssueClient(issue_http, REPO),
            ollama=OllamaClient(ollama_http, "http://ollama:11434", "m"),
            telegram=self.telegram.append,
            limiter=RateLimiter(limit),
            now=lambda: NOW,
        )

    def _probe(self, url):
        self.probe_calls.append(url)
        return PROBE


def test_pipeline_create_then_analysis():
    s = Setup()
    handle_event(_event(), s.deps)
    created = s.store.calls("POST", "/issues")
    assert len(created) == 1
    assert created[0]["labels"] == ["incident", "project:videoflix", "monitor:videoflix-api"]
    assert "Analysis pending." in created[0]["body"]
    comments = s.store.calls("POST", "/comments")
    assert len(comments) == 1
    assert "Analysis (suspected cause)" in comments[0]["body"]
    assert len(s.telegram) == 1
    assert "https://github.com/owner/issues/issues/1" in s.telegram[0]
    assert s.probe_calls == ["https://x.example/health"]


def test_pipeline_llm_switch_off():
    s = Setup(llm=False)
    handle_event(_event(), s.deps)
    assert s.ollama_calls == 0
    assert "no LLM analysis" in s.store.calls("POST", "/issues")[0]["body"]
    assert s.store.calls("POST", "/comments") == []
    assert len(s.telegram) == 1
    assert "upstream_502" in s.telegram[0]


def test_pipeline_project_without_llm():
    s = Setup()
    handle_event(_event("Portfolio"), s.deps)
    assert s.ollama_calls == 0
    assert len(s.telegram) == 1
    assert "Portfolio" in s.telegram[0]


def test_pipeline_github_down_skips_llm():
    s = Setup(github_down=True)
    handle_event(_event(), s.deps)
    assert s.ollama_calls == 0
    body = s.store.calls("POST", "/issues")[0]["body"]
    assert "### Rule result" in body
    assert len(s.telegram) == 1


def test_pipeline_rate_limit_skips_llm():
    s = Setup(limit=0)
    handle_event(_event(), s.deps)
    assert s.ollama_calls == 0
    comments = s.store.calls("POST", "/comments")
    assert len(comments) == 1
    assert "hourly limit reached" in comments[0]["body"]
    assert len(s.telegram) == 1


def test_pipeline_unavailable_comment():
    s = Setup(ollama_status=500)
    handle_event(_event(), s.deps)
    comments = s.store.calls("POST", "/comments")
    assert len(comments) == 1
    assert "Analysis unavailable" in comments[0]["body"]
    assert len(s.telegram) == 1


def test_pipeline_append_adds_label_no_telegram():
    s = Setup()
    s.store.seed(7, ["incident", "project:videoflix", "monitor:videoflix-web-app"])
    handle_event(_event("Videoflix · API"), s.deps)
    assert "Additional alert" in s.store.calls("POST", "/comments")[0]["body"]
    assert s.store.calls("POST", "/labels") == [{"labels": ["monitor:videoflix-api"]}]
    assert s.ollama_calls == 0
    assert s.telegram == []


def test_pipeline_reopen_flap():
    s = Setup()
    s.store.seed(
        7,
        ["incident", "project:videoflix", "resolved-pending-review"],
        updated_at=NOW - timedelta(minutes=3),
    )
    handle_event(_event(), s.deps)
    assert "Flapping: down again" in s.store.calls("POST", "/comments")[0]["body"]
    deletes = [p for m, p, _ in s.store.requests if m == "DELETE"]
    assert deletes == ["/issues/7/labels/resolved-pending-review"]
    assert s.store.calls("POST", "/labels") == [{"labels": ["monitor:videoflix-api"]}]
    assert s.telegram == []


def _counting_reader(calls: list) -> GitHubReader:
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[])

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubReader(client)


def test_pipeline_append_and_flap_skip_github_read():
    for labels, updated in (
        (["incident", "project:videoflix", "monitor:videoflix-web-app"], None),
        (["incident", "project:videoflix", "resolved-pending-review"], NOW - timedelta(minutes=3)),
    ):
        s = Setup()
        reads = []
        s.deps.reader = _counting_reader(reads)
        s.store.seed(7, labels, updated_at=updated)
        handle_event(_event(), s.deps)
        assert len(s.store.calls("POST", "/comments")) == 1
        assert reads == []


def test_pipeline_monitor_up_with_other_monitor_down():
    s = Setup()
    s.store.seed(
        7,
        ["incident", "project:videoflix", "monitor:videoflix-api", "monitor:videoflix-web-app"],
    )
    handle_event(_event("Videoflix · API", "up"), s.deps)
    assert (
        "recovered; other monitors are still down" in s.store.calls("POST", "/comments")[0]["body"]
    )
    deletes = [p for m, p, _ in s.store.requests if m == "DELETE"]
    assert deletes == ["/issues/7/labels/monitor:videoflix-api"]
    assert s.probe_calls == []
    assert s.telegram == []


def test_pipeline_resolve_no_telegram():
    s = Setup()
    s.store.seed(7, ["incident", "project:videoflix", "monitor:videoflix-api"])
    handle_event(_event("Videoflix · API", "up"), s.deps)
    assert (
        "Recovered 5 min after the incident started"
        in s.store.calls("POST", "/comments")[0]["body"]
    )
    assert s.store.calls("POST", "/labels") == [{"labels": ["resolved-pending-review"]}]
    deletes = [p for m, p, _ in s.store.requests if m == "DELETE"]
    assert deletes == ["/issues/7/labels/monitor:videoflix-api"]
    assert s.telegram == []


def test_pipeline_ignores_duplicate_down():
    s = Setup()
    s.store.seed(7, ["incident", "project:videoflix", "monitor:videoflix-api"])
    handle_event(_event("Videoflix · API"), s.deps)
    assert [m for m, _, _ in s.store.requests] == ["GET"]
    assert s.probe_calls == []


def test_pipeline_appends_same_monitor_down_after_window():
    s = Setup()
    s.store.seed(
        7,
        ["incident", "project:videoflix", "monitor:videoflix-api"],
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(minutes=11),
    )
    handle_event(_event("Videoflix · API"), s.deps)
    assert "Additional alert" in s.store.calls("POST", "/comments")[0]["body"]
    assert s.store.calls("POST", "/issues") == []
    assert s.telegram == []


def test_issue_unavailable_is_logged_and_swallowed(caplog):
    s = Setup()
    s.deps.issues = IssueClient(
        httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        ),
        REPO,
    )
    handle_event(_event(), s.deps)
    assert s.telegram == []
    assert "IssueUnavailable" in caplog.text


def test_telegram_failure_does_not_stop(caplog):
    s = Setup(llm=False)

    def failing(text):
        raise TelegramError("Telegram request failed: HTTP 500")

    s.deps.telegram = failing
    handle_event(_event(), s.deps)
    assert len(s.store.calls("POST", "/issues")) == 1
    assert "Telegram" in caplog.text


def test_run_event_logs_unexpected_error(caplog):
    s = Setup(llm=False)

    def boom(text):
        raise RuntimeError("garbage")

    s.deps.telegram = boom
    run_event(_event(), s.deps)
    assert "RuntimeError" in caplog.text


def test_probe_error_still_creates_issue(caplog):
    s = Setup(llm=False)

    def boom(url):
        # getaddrinfo raises UnicodeError for an empty or overlong label.
        raise UnicodeError("label too long")

    s.deps.probe = boom
    handle_event(_event(), s.deps)
    body = s.store.calls("POST", "/issues")[0]["body"]
    assert "- F1: Kuma:" in body and "- F2:" not in body
    assert len(s.telegram) == 1
    assert "UnicodeError" in caplog.text


def test_concurrent_downs_create_one_issue():
    s = Setup(delay=0.05)
    events = [_event("Videoflix · Web-App"), _event("Videoflix · API")]
    threads = [threading.Thread(target=handle_event, args=(e, s.deps)) for e in events]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s.store.calls("POST", "/issues")) == 1


def test_comment_failure_still_sends_one_telegram(caplog):
    s = Setup()
    original = s.store.handler

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/comments"):
            return httpx.Response(500)
        return original(request)

    s.deps.issues = IssueClient(
        httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)),
        REPO,
    )
    handle_event(_event(), s.deps)
    assert len(s.store.calls("POST", "/issues")) == 1
    assert len(s.telegram) == 1


def test_analyze_error_still_sends_one_telegram(monkeypatch, caplog):
    s = Setup()

    def broken(ctx, ollama):
        raise KeyError("bug")

    monkeypatch.setattr("assistant.pipeline.analyze", broken)
    handle_event(_event(), s.deps)
    assert len(s.telegram) == 1
    assert "KeyError" in caplog.text


def test_analyze_error_resolves_analysis_pending(monkeypatch, caplog):
    s = Setup()

    def broken(ctx, ollama):
        raise KeyError("bug")

    monkeypatch.setattr("assistant.pipeline.analyze", broken)
    handle_event(_event(), s.deps)
    comments = s.store.calls("POST", "/comments")
    assert [c["body"] for c in comments] == [
        "Analysis unavailable: internal error. The rule result above still applies."
    ]
    assert len(s.telegram) == 1


def test_analyze_error_with_failing_comment_still_sends_telegram(monkeypatch, caplog):
    s = Setup()
    original = s.store.handler

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/comments"):
            return httpx.Response(500)
        return original(request)

    def broken(ctx, ollama):
        raise KeyError("bug")

    monkeypatch.setattr("assistant.pipeline.analyze", broken)
    s.deps.issues = IssueClient(
        httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)),
        REPO,
    )
    handle_event(_event(), s.deps)
    assert len(s.telegram) == 1
    assert "KeyError" in caplog.text and "IssueUnavailable" in caplog.text
