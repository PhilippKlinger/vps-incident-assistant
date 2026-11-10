from pathlib import Path

from assistant.runbook import github_slug, parse_runbook

FIXTURE = Path(__file__).parent / "fixtures" / "readme_vps_edge.md"


def test_slug_examples():
    assert github_slug("Upstream 502") == "upstream-502"
    assert github_slug("Database or cache not ok") == "database-or-cache-not-ok"
    assert github_slug("API 5xx") == "api-5xx"
    assert github_slug("What's up? (v2)") == "whats-up-v2"


def test_parse_runbook_vps_edge():
    anchors = parse_runbook("PhilippKlinger/vps-edge", FIXTURE.read_text(encoding="utf-8"))

    assert [a.id for a in anchors] == [
        "vps-edge#proxy-down",
        "vps-edge#upstream-502",
        "vps-edge#certificate",
    ]
    assert anchors[1].repo == "PhilippKlinger/vps-edge"
    assert anchors[1].title == "Upstream 502"
    assert anchors[1].text
    assert "###" not in anchors[1].text


def test_parse_runbook_without_section():
    assert parse_runbook("o/r", "# T\n\n## Usage\n\n### Not a runbook\ntext\n") == []


def test_parse_runbook_ignores_fenced_headings_and_stops_at_next_section():
    md = (
        "## Troubleshooting\n\n### A\nbody a\n```\n### not a heading\n```\n\n"
        "### B\nbody b\n\n## Other\n\n### C\nbody c\n"
    )
    anchors = parse_runbook("o/r", md)

    assert [a.id for a in anchors] == ["r#a", "r#b"]
    assert "### not a heading" in anchors[0].text
    assert anchors[1].text == "body b"
