from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from assistant.lifecycle import INCIDENT, Issue, project_label

_SHAPE_ERRORS = (ValueError, KeyError, TypeError)


class IssueUnavailable(Exception):
    """The GitHub issue could not be read or written."""


def make_issue_client(token: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
        transport=transport,
    )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _to_issue(item: dict) -> Issue:
    return Issue(
        number=item["number"],
        labels=frozenset(label["name"] for label in item["labels"]),
        created_at=_parse_time(item["created_at"]),
        updated_at=_parse_time(item["updated_at"]),
    )


class IssueClient:
    def __init__(self, client: httpx.Client, repo: str):
        self._client = client
        self._repo = repo

    def _request(self, method: str, path: str, *, ok_404: bool = False, **kwargs) -> httpx.Response:
        # Error messages carry only the status or exception type, never headers (token).
        try:
            response = self._client.request(method, f"/repos/{self._repo}{path}", **kwargs)
        except httpx.TransportError as exc:
            raise IssueUnavailable(f"GitHub not reachable: {type(exc).__name__}") from None
        if response.status_code == 404 and ok_404:
            return response
        if not response.is_success:
            raise IssueUnavailable(f"GitHub issue request failed: HTTP {response.status_code}")
        return response

    def find_open(self, project: str) -> Issue | None:
        response = self._request(
            "GET",
            "/issues",
            params={
                "labels": f"{INCIDENT},{project_label(project)}",
                "state": "open",
                "sort": "created",
                "direction": "desc",
                "per_page": 1,
            },
        )
        try:
            items = response.json()
            return _to_issue(items[0]) if items else None
        except _SHAPE_ERRORS:
            raise IssueUnavailable("GitHub returned an unexpected response") from None

    def create(self, title: str, body: str, labels: list[str]) -> Issue:
        response = self._request(
            "POST", "/issues", json={"title": title, "body": body, "labels": labels}
        )
        try:
            return _to_issue(response.json())
        except _SHAPE_ERRORS:
            raise IssueUnavailable("GitHub returned an unexpected response") from None

    def comment(self, number: int, body: str) -> None:
        self._request("POST", f"/issues/{number}/comments", json={"body": body})

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._request("POST", f"/issues/{number}/labels", json={"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        # 404 means the label is already gone, which is the goal.
        self._request("DELETE", f"/issues/{number}/labels/{quote(label, safe='')}", ok_404=True)
