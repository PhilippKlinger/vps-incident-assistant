import re

from assistant.models import RunbookAnchor

_SECTION = "## Troubleshooting"


def github_slug(heading: str) -> str:
    """Anchor of a heading the way GitHub builds it."""
    kept = re.sub(r"[^\w \-]", "", heading.lower())
    return kept.replace(" ", "-")


def parse_runbook(repo: str, readme_md: str) -> list[RunbookAnchor]:
    """Anchors for the ### headings under '## Troubleshooting' of a RUNBOOK.md."""
    name = repo.split("/")[-1]
    anchors: list[RunbookAnchor] = []
    in_section = False
    in_fence = False
    title: str | None = None
    body: list[str] = []

    def close() -> None:
        if title is not None:
            anchors.append(
                RunbookAnchor(
                    id=f"{name}#{github_slug(title)}",
                    repo=repo,
                    title=title,
                    text="\n".join(body).strip(),
                )
            )

    for line in readme_md.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        heading = not in_fence and re.match(r"#{1,3} ", line)
        if heading and not line.startswith("### "):
            close()
            title, body = None, []
            in_section = line.strip() == _SECTION
        elif heading and in_section:
            close()
            title, body = line[4:].strip(), []
        elif title is not None:
            body.append(line)
    close()
    return anchors
