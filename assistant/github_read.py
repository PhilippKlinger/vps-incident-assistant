from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

_MARKER = "\n… truncated"
# Malformed JSON or unexpected shapes are treated like an outage.
_SHAPE_ERRORS = (ValueError, KeyError, TypeError)


class GitHubUnavailable(Exception):
    """GitHub could not be read (network, 5xx, rate limit, auth)."""


@dataclass(frozen=True)
class RawChange:
    repo: str
    sha: str
    title: str
    author: str
    merged_at: datetime
    files: tuple[str, ...]
    patch: str
    body: str


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )


def truncate_patch(patch: str, max_chars: int = 2000) -> str:
    if len(patch) <= max_chars:
        return patch
    cut = patch[: max_chars - len(_MARKER)]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    return cut + _MARKER


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class GitHubReader:
    def __init__(self, client: httpx.Client):
        self._client = client

    def _get(self, path: str, *, ok_404: bool = False, **kwargs) -> httpx.Response | None:
        # Error messages carry only the status or exception type, never headers (token).
        # HTTPError, not TransportError: a DecodingError is not a transport error.
        try:
            response = self._client.get(path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubUnavailable(f"GitHub not reachable: {type(exc).__name__}") from None
        status = response.status_code
        if 300 <= status < 400:
            # Redirects are not followed; a moved repo must not yield a half-read answer.
            raise GitHubUnavailable(f"GitHub redirected: HTTP {status}")
        if status == 404 and ok_404:
            return None
        rate_limited = status == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        if status >= 500 or status == 429 or rate_limited:
            raise GitHubUnavailable(f"GitHub unavailable: HTTP {status}")
        if status >= 400:
            # Other 4xx (bad token, wrong repo) use the same error so callers handle one type.
            raise GitHubUnavailable(f"GitHub request failed: HTTP {status}")
        return response

    def recent_changes(self, repo: str, since: datetime, limit: int = 10) -> list[RawChange]:
        try:
            return self._recent_changes(repo, since, limit)
        except _SHAPE_ERRORS:
            raise GitHubUnavailable("GitHub returned an unexpected response") from None

    def _recent_changes(self, repo: str, since: datetime, limit: int) -> list[RawChange]:
        # Walk the first-parent chain of main: a merge commit is one merged PR, its
        # committer date is when the work landed. The list endpoint has no first-parent
        # option and filters by branch commit dates, so each commit is fetched by sha.
        changes = []
        ref = "main"
        while len(changes) < limit:
            detail = self._get(f"/repos/{repo}/commits/{ref}").json()
            when = _parse_time(detail["commit"]["committer"]["date"])
            if when < since:
                break
            changes.append(self._change(repo, when, detail))
            parents = detail.get("parents", [])
            if not parents:
                break
            ref = parents[0]["sha"]
        return changes

    def _change(self, repo: str, when: datetime, detail: dict) -> RawChange:
        sha = detail["sha"]
        files = detail.get("files", [])
        patch = "\n".join(
            f"diff --git a/{f['filename']} b/{f['filename']}\n{f.get('patch', '')}".rstrip("\n")
            for f in files
        )
        head, _, body = detail["commit"]["message"].partition("\n")
        author = (detail.get("author") or {}).get("login") or detail["commit"]["author"]["name"]
        return RawChange(
            repo=repo,
            sha=sha,
            title=head.strip(),
            author=author,
            merged_at=when,
            files=tuple(f["filename"] for f in files),
            patch=patch,
            body=body.strip(),
        )

    def deploy_runs(self, repo: str, since: datetime) -> list[str]:
        response = self._get(
            f"/repos/{repo}/actions/workflows/deploy.yml/runs",
            ok_404=True,
            params={"created": f">={since.date().isoformat()}", "per_page": 10},
        )
        if response is None:
            return []
        try:
            return [_run_line(run) for run in response.json()["workflow_runs"]]
        except _SHAPE_ERRORS:
            raise GitHubUnavailable("GitHub returned an unexpected response") from None

    def readme(self, repo: str) -> str:
        response = self._get(
            f"/repos/{repo}/readme", headers={"Accept": "application/vnd.github.raw+json"}
        )
        return response.text


def _run_line(run: dict) -> str:
    when = _parse_time(run["created_at"]).strftime("%Y-%m-%d %H:%M")
    result = run.get("conclusion") or run.get("status")
    return f"Deploy {when} UTC: {result} ({run['event']}, {run['display_title']})"
