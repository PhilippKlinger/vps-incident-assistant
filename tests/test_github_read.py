from datetime import UTC, datetime, timedelta

import httpx
import pytest

from assistant.github_read import GitHubReader, GitHubUnavailable, make_client, truncate_patch

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _reader(handler) -> GitHubReader:
    return GitHubReader(
        httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    )


def _commit(sha, when, login="alice"):
    return {
        "sha": sha,
        "commit": {
            "message": f"fix: change {sha}\n\nlonger body",
            "committer": {"date": when.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "author": {"name": "Alice A"},
        },
        "author": {"login": login} if login else None,
    }


def test_truncate_patch_line_boundary():
    patch = "\n".join(f"line {i:03d}" for i in range(100))

    out = truncate_patch(patch, max_chars=100)

    assert len(out) <= 100
    assert out.endswith("\n… gekürzt")
    body = out.removesuffix("\n… gekürzt")
    assert body.split("\n")[-1].startswith("line ") and len(body.split("\n")[-1]) == 8
    assert truncate_patch("short", 100) == "short"


def test_recent_changes_respects_limit_and_since():
    since = NOW - timedelta(hours=72)
    commits = [_commit(f"s{i:02d}", NOW - timedelta(hours=i)) for i in range(10)]
    commits += [
        _commit("old1", since - timedelta(hours=1)),
        _commit("old2", since - timedelta(hours=2)),
    ]
    commits[1] = _commit("s01", NOW - timedelta(hours=1), login=None)
    calls = []

    def handler(request):
        calls.append(request.url)
        if request.url.path == "/repos/o/r/commits":
            assert request.url.params["sha"] == "main"
            assert request.url.params["per_page"] == "10"
            return httpx.Response(200, json=commits)
        return httpx.Response(
            200,
            json={
                "files": [
                    {"filename": "a.txt", "patch": "@@ -1 +1 @@\n-x\n+y"},
                    {"filename": "b.png"},
                ]
            },
        )

    changes = _reader(handler).recent_changes("o/r", since)

    assert len(changes) == 10
    assert all(c.merged_at >= since for c in changes)
    assert [c.merged_at for c in changes] == sorted((c.merged_at for c in changes), reverse=True)
    first = changes[0]
    assert first.sha == "s00"
    assert first.author == "alice"
    assert first.title == "fix: change s00"
    assert first.body == "longer body"
    assert first.files == ("a.txt", "b.png")
    assert "diff --git a/a.txt b/a.txt" in first.patch
    assert "+y" in first.patch
    assert changes[1].author == "Alice A"


def test_rate_limit_raises_unavailable():
    def handler(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={})

    with pytest.raises(GitHubUnavailable):
        _reader(handler).readme("o/r")


def test_server_error_and_network_error_raise_unavailable():
    def boom(request):
        raise httpx.ConnectTimeout("t")

    with pytest.raises(GitHubUnavailable):
        _reader(boom).readme("o/r")
    with pytest.raises(GitHubUnavailable):
        _reader(lambda r: httpx.Response(502)).readme("o/r")
    with pytest.raises(GitHubUnavailable):
        _reader(lambda r: httpx.Response(429)).readme("o/r")


def test_readme_requests_raw():
    def handler(request):
        assert request.url.path == "/repos/o/r/readme"
        assert request.headers["accept"] == "application/vnd.github.raw+json"
        return httpx.Response(200, text="# Hi")

    assert _reader(handler).readme("o/r") == "# Hi"


def test_deploy_runs_lines_and_missing_workflow():
    def handler(request):
        assert request.url.path == "/repos/o/r/actions/workflows/deploy.yml/runs"
        assert request.url.params["created"] == ">=2026-11-07"
        return httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "created_at": "2026-11-10T13:05:00Z",
                        "conclusion": "success",
                        "status": "completed",
                        "event": "workflow_dispatch",
                        "display_title": "Deploy backend",
                    },
                    {
                        "created_at": "2026-11-10T13:10:00Z",
                        "conclusion": None,
                        "status": "in_progress",
                        "event": "push",
                        "display_title": "x",
                    },
                ]
            },
        )

    lines = _reader(handler).deploy_runs("o/r", NOW - timedelta(hours=72))

    assert lines[0] == "Deploy 2026-11-10 13:05 UTC: success (workflow_dispatch, Deploy backend)"
    assert "in_progress" in lines[1]
    assert _reader(lambda r: httpx.Response(404)).deploy_runs("o/r", NOW) == []


def test_make_client_headers():
    client = make_client("secret-token")

    assert str(client.base_url).startswith("https://api.github.com")
    assert client.headers["authorization"] == "Bearer secret-token"
    assert client.headers["x-github-api-version"] == "2022-11-28"
    assert client.headers["accept"] == "application/vnd.github+json"
    assert client.timeout.read == 10


def test_unavailable_message_has_no_token():
    client = make_client("secret-token")
    client._transport = httpx.MockTransport(lambda r: httpx.Response(500))
    with pytest.raises(GitHubUnavailable) as exc:
        GitHubReader(client).readme("o/r")
    assert "secret-token" not in str(exc.value)
