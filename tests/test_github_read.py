from datetime import UTC, datetime, timedelta

import httpx
import pytest

from assistant.github_read import GitHubReader, GitHubUnavailable, make_client, truncate_patch

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _reader(handler) -> GitHubReader:
    return GitHubReader(
        httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    )


def _commit(sha, when, parents=(), login="alice", files=()):
    return {
        "sha": sha,
        "parents": [{"sha": p} for p in parents],
        "commit": {
            "message": f"fix: change {sha}\n\nlonger body",
            "committer": {"date": when.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "author": {"name": "Alice A"},
        },
        "author": {"login": login} if login else None,
        "files": list(files),
    }


def _chain_handler(commits, calls):
    by_sha = {c["sha"]: c for c in commits}
    head = commits[0]

    def handler(request):
        calls.append(request.url.path)
        prefix = "/repos/o/r/commits/"
        assert request.url.path.startswith(prefix)
        ref = request.url.path.removeprefix(prefix)
        commit = head if ref == "main" else by_sha.get(ref)
        if commit is None:
            return httpx.Response(404)
        return httpx.Response(200, json=commit)

    return handler


def test_truncate_patch_line_boundary():
    patch = "\n".join(f"line {i:03d}" for i in range(100))

    out = truncate_patch(patch, max_chars=100)

    assert len(out) <= 100
    assert out.endswith("\n… truncated")
    body = out.removesuffix("\n… truncated")
    assert body.split("\n")[-1].startswith("line ") and len(body.split("\n")[-1]) == 8
    assert truncate_patch("short", 100) == "short"


def test_recent_changes_lists_merge_commit_not_its_branch():
    files = [{"filename": "a.txt", "patch": "@@ -1 +1 @@\n-x\n+y"}, {"filename": "b.png"}]
    merge = _commit("merge", NOW - timedelta(hours=1), parents=["base", "branch2"], files=files)
    base = _commit("base", NOW - timedelta(hours=30))
    branch2 = _commit("branch2", NOW - timedelta(hours=40), parents=["base"])
    calls = []

    changes = _reader(_chain_handler([merge, base, branch2], calls)).recent_changes(
        "o/r", NOW - timedelta(hours=72)
    )

    assert [c.sha for c in changes] == ["merge", "base"]
    first = changes[0]
    assert first.sha == "merge"
    assert first.merged_at == NOW - timedelta(hours=1)
    assert first.files == ("a.txt", "b.png")
    assert "diff --git a/a.txt b/a.txt" in first.patch
    assert "+y" in first.patch
    assert first.title == "fix: change merge"
    assert first.body == "longer body"
    assert "/repos/o/r/commits/branch2" not in calls


def test_recent_changes_stops_at_since_and_limit():
    since = NOW - timedelta(hours=72)
    commits = [
        _commit(f"s{i:02d}", NOW - timedelta(hours=i), parents=[f"s{i + 1:02d}"], login=None)
        for i in range(9)
    ]
    commits += [
        _commit("old1", since - timedelta(hours=1), parents=["old2"]),
        _commit("old2", since - timedelta(hours=2), parents=["old3"]),
    ]
    commits[8]["parents"] = [{"sha": "old1"}]
    calls = []

    changes = _reader(_chain_handler(commits, calls)).recent_changes("o/r", since)

    assert [c.sha for c in changes] == [f"s{i:02d}" for i in range(9)]
    assert changes[0].author == "Alice A"
    assert calls[0] == "/repos/o/r/commits/main"
    assert calls[-1] == "/repos/o/r/commits/old1"
    assert len(calls) == 10

    calls.clear()
    limited = _reader(_chain_handler(commits, calls)).recent_changes("o/r", since, limit=3)

    assert [c.sha for c in limited] == ["s00", "s01", "s02"]
    assert len(calls) == 3


def test_recent_changes_stops_at_root_commit():
    tip = _commit("tip", NOW - timedelta(hours=1), parents=["root"])
    root = _commit("root", NOW - timedelta(hours=2))
    calls = []

    changes = _reader(_chain_handler([tip, root], calls)).recent_changes(
        "o/r", NOW - timedelta(hours=72)
    )

    assert [c.sha for c in changes] == ["tip", "root"]
    assert len(calls) == 2


def test_recent_changes_lists_direct_push():
    push = _commit("push", NOW - timedelta(hours=1), parents=["base"], files=[{"filename": "x.py"}])
    base = _commit("base", NOW - timedelta(hours=100))

    changes = _reader(_chain_handler([push, base], [])).recent_changes(
        "o/r", NOW - timedelta(hours=72)
    )

    assert [c.sha for c in changes] == ["push"]
    assert changes[0].files == ("x.py",)


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


def test_redirect_raises_unavailable():
    reader = _reader(lambda request: httpx.Response(301, headers={"location": "https://x.test/"}))

    with pytest.raises(GitHubUnavailable):
        reader.readme("o/r")


def test_malformed_json_raises_unavailable():
    since = NOW - timedelta(hours=72)
    reader = _reader(lambda request: httpx.Response(200, content=b"<html>not json"))

    with pytest.raises(GitHubUnavailable) as info:
        reader.recent_changes("o/r", since)
    assert "Bearer" not in str(info.value)
    with pytest.raises(GitHubUnavailable):
        reader.deploy_runs("o/r", since)
    shaped = _reader(lambda request: httpx.Response(200, json={"unexpected": 1}))
    with pytest.raises(GitHubUnavailable):
        shaped.deploy_runs("o/r", since)


def test_decoding_error_raises_unavailable():
    # DecodingError is an HTTPError but not a TransportError.
    def handler(request):
        raise httpx.DecodingError("broken gzip")

    with pytest.raises(GitHubUnavailable, match="DecodingError"):
        _reader(handler).readme("o/r")
