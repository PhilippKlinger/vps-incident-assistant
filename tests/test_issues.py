import json
from datetime import UTC, datetime

import httpx
import pytest

from assistant.issues import IssueClient, IssueUnavailable, make_issue_client

ISSUE = {
    "number": 12,
    "labels": [{"name": "incident"}, {"name": "monitor:videoflix-api"}],
    "created_at": "2026-11-10T11:00:00Z",
    "updated_at": "2026-11-10T11:30:00Z",
}


def _client(handler) -> IssueClient:
    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return IssueClient(http, "owner/repo")


def test_find_open_queries_labels():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[ISSUE])

    issue = _client(handler).find_open("videoflix")
    query = seen[0].url.params
    assert seen[0].url.path == "/repos/owner/repo/issues"
    assert query["labels"] == "incident,project:videoflix"
    assert query["state"] == "open"
    assert query["sort"] == "created"
    assert query["direction"] == "desc"
    assert query["per_page"] == "1"
    assert issue.number == 12
    assert issue.labels == frozenset({"incident", "monitor:videoflix-api"})
    assert issue.created_at == datetime(2026, 11, 10, 11, 0, tzinfo=UTC)
    assert issue.updated_at == datetime(2026, 11, 10, 11, 30, tzinfo=UTC)


def test_find_open_returns_none_when_empty():
    assert _client(lambda r: httpx.Response(200, json=[])).find_open("videoflix") is None


def test_create_sends_labels():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json=ISSUE)

    issue = _client(handler).create("Titel", "Text", ["incident", "project:videoflix"])
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/repos/owner/repo/issues"
    assert json.loads(seen[0].content) == {
        "title": "Titel",
        "body": "Text",
        "labels": ["incident", "project:videoflix"],
    }
    assert issue.number == 12


def test_comment_posts_body():
    seen = []
    _client(lambda r: (seen.append(r), httpx.Response(201, json={}))[1]).comment(12, "Hallo")
    assert seen[0].url.path == "/repos/owner/repo/issues/12/comments"
    assert json.loads(seen[0].content) == {"body": "Hallo"}


def test_add_labels_posts_list():
    seen = []
    _client(lambda r: (seen.append(r), httpx.Response(200, json=[]))[1]).add_labels(12, ["a", "b"])
    assert seen[0].url.path == "/repos/owner/repo/issues/12/labels"
    assert json.loads(seen[0].content) == {"labels": ["a", "b"]}


def test_remove_label_encodes_and_tolerates_404():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(404)

    _client(handler).remove_label(12, "monitor:videoflix-api")
    assert seen[0].method == "DELETE"
    assert (
        seen[0].url.raw_path.decode()
        == "/repos/owner/repo/issues/12/labels/monitor%3Avideoflix-api"
    )


def test_issue_token_is_used():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    http = make_issue_client("issue-token", transport=httpx.MockTransport(handler))
    IssueClient(http, "owner/repo").find_open("videoflix")
    assert seen[0].headers["Authorization"] == "Bearer issue-token"


def test_http_error_raises_without_token():
    client = _client(lambda r: httpx.Response(403))
    with pytest.raises(IssueUnavailable, match="403"):
        client.comment(12, "x")


def test_remove_label_server_error_raises():
    with pytest.raises(IssueUnavailable, match="500"):
        _client(lambda r: httpx.Response(500)).remove_label(12, "x")


def test_transport_error_raises_type_only():
    def handler(request):
        raise httpx.ConnectError("secret detail")

    with pytest.raises(IssueUnavailable) as info:
        _client(handler).find_open("videoflix")
    assert "ConnectError" in str(info.value)
    assert "secret" not in str(info.value)


def test_decoding_error_raises_unavailable():
    # DecodingError is an HTTPError but not a TransportError.
    def handler(request):
        raise httpx.DecodingError("broken gzip")

    with pytest.raises(IssueUnavailable, match="DecodingError"):
        _client(handler).comment(12, "x")
