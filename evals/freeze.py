import json
from datetime import UTC, datetime
from pathlib import Path

from assistant.github_read import GitHubReader, truncate_patch

PATCH_CHARS = 2000
_SECTION = "## Troubleshooting"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _key(repo: str) -> str:
    return repo.replace("/", "__")


def troubleshooting_section(readme_md: str) -> str:
    """The '## Troubleshooting' section of a README, up to the next '## ' heading."""
    lines: list[str] = []
    in_section = False
    in_fence = False
    for line in readme_md.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            in_section = line.strip() == _SECTION
        if in_section:
            lines.append(line)
    return "\n".join(lines).strip() + "\n"


def freeze(
    reader: GitHubReader,
    repos: list[str],
    runbook_repos: list[str],
    out: Path,
    per_repo: int = 40,
) -> None:
    """Write the last commits of each repo and the runbook sections as eval data."""
    (out / "history").mkdir(parents=True, exist_ok=True)
    (out / "runbooks").mkdir(parents=True, exist_ok=True)
    for repo in repos:
        # The reader walks the first-parent chain of main (merge commits = merged PRs).
        # Re-freezing changes the SHAs (merge instead of branch commits), so cases would
        # have to be re-anchored.
        changes = reader.recent_changes(repo, _EPOCH, limit=per_repo)
        entries = [
            {
                "repo": change.repo,
                "sha": change.sha,
                "title": change.title,
                "author": change.author,
                "committed_at": change.merged_at.isoformat(),
                "files": list(change.files),
                "patch": truncate_patch(change.patch, PATCH_CHARS),
                "body": change.body,
            }
            for change in changes
        ]
        path = out / "history" / f"{_key(repo)}.json"
        text = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
    for repo in runbook_repos:
        section = troubleshooting_section(reader.readme(repo))
        path = out / "runbooks" / f"{_key(repo)}.md"
        path.write_text(section, encoding="utf-8", newline="\n")
