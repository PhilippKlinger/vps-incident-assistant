import json

import httpx

from assistant.github_read import GitHubReader
from evals.cases import HistoryStore
from evals.freeze import freeze

REPO = "acme/app"
README = """# App

## Usage

Run it.

## Troubleshooting

### Port busy

Stop the other process.

## License

MIT
"""


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    single = {
        "sha": "a" * 40,
        "parents": [],
        "commit": {
            "message": "fix: port\n\nbody text",
            "committer": {"date": "2026-11-01T10:00:00Z"},
            "author": {"name": "Alice A"},
        },
        "author": {"login": "alice"},
        "files": [{"filename": "app.py", "patch": "+" + "x" * 5000}],
    }
    if path == f"/repos/{REPO}/commits/main":
        merge = {**single, "sha": "c" * 40, "parents": [{"sha": "a" * 40}, {"sha": "d" * 40}]}
        return httpx.Response(200, json=merge)
    if path == f"/repos/{REPO}/commits/{'a' * 40}":
        return httpx.Response(200, json=single)
    if path == f"/repos/{REPO}/contents/RUNBOOK.md":
        return httpx.Response(200, text=README)
    return httpx.Response(404)


def test_freeze_writes_history_and_runbook(tmp_path):
    reader = GitHubReader(
        httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(_handler))
    )

    freeze(reader, [REPO], [REPO], tmp_path)

    entries = json.loads((tmp_path / "history" / "acme__app.json").read_text(encoding="utf-8"))
    assert [e["sha"] for e in entries] == ["c" * 40, "a" * 40]
    entry = entries[1]
    assert entry["repo"] == REPO
    assert entry["sha"] == "a" * 40
    assert entry["title"] == "fix: port"
    assert entry["author"] == "alice"
    assert entry["committed_at"] == "2026-11-01T10:00:00+00:00"
    assert entry["files"] == ["app.py"]
    assert entry["body"] == "body text"
    assert len(entry["patch"]) <= 2000
    runbook = (tmp_path / "runbooks" / "acme__app.md").read_text(encoding="utf-8")
    assert "### Port busy" in runbook
    assert "Usage" not in runbook and "License" not in runbook
    store = HistoryStore.load(tmp_path)
    assert store.commit(REPO, "a" * 7)["title"] == "fix: port"
