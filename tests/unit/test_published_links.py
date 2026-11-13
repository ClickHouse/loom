"""Absolute links that published files carry (registry listing, plugin manifests, package
metadata and READMEs, the citation, the chart, the issue templates) resolve to files in this
repository; the doc-link check follows only relative links."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE = ROOT / "integrations" / "claude-code-plugin"
CODEX = ROOT / "integrations" / "codex-plugin"

SITE = "https://clickhouse.github.io/loom/"


def _site_page(url: str) -> Path:
    """The docs/ source of a documentation-site URL: `<site>/a/b/` is `docs/a/b.md`, or
    `docs/a/b/index.md` for a section's index page."""
    path = url.removeprefix(SITE).partition("#")[0].strip("/")
    page = ROOT / "docs" / f"{path}.md"
    return page if path and page.is_file() else ROOT / "docs" / path / "index.md"


def test_both_plugins_link_the_data_handling_section() -> None:
    """The privacy link of both listings is an absolute URL, which the doc-link check does
    not follow; renaming the section would leave both listings pointing at nothing."""
    claude = json.loads((CLAUDE / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((CODEX / ".codex-plugin" / "plugin.json").read_text())
    url = claude["privacyPolicyUrl"]
    assert url == codex["interface"]["privacyPolicyURL"]
    assert url.startswith(SITE) and url.endswith("#data-handling")
    assert "\n## Data handling\n" in _site_page(url).read_text()


def test_published_links_resolve() -> None:
    """The registry listing, the plugin manifests, both pyproject files, the SDK packages
    and their READMEs, the citation, the chart and the issue templates publish absolute
    repository and site links, which the doc-link check does not follow; a renamed file
    breaks them."""
    text = "\n".join(
        (ROOT / f).read_text()
        for f in (
            "server.json",
            ".claude-plugin/plugin.json",
            "integrations/claude-code-plugin/.claude-plugin/plugin.json",
            "integrations/codex-plugin/.codex-plugin/plugin.json",
            "pyproject.toml",
            "sdk/python/pyproject.toml",
            "sdk/typescript/package.json",
            "CITATION.cff",
            "deploy/helm/loom/Chart.yaml",
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/ISSUE_TEMPLATE/10_question.yaml",
            ".github/ISSUE_TEMPLATE/80_documentation-issue.yaml",
            "sdk/python/README.md",
            "sdk/typescript/README.md",
        )
    )
    links = re.findall(
        r"https://(?:github\.com/ClickHouse/loom/blob/main|raw\.githubusercontent\.com/ClickHouse/loom/main)/([^\"#\s)]+)",
        text,
    )
    site_links = re.findall(re.escape(SITE) + r"[^\"\s)]*", text)
    assert links and site_links
    for path in links:
        assert (ROOT / path).is_file(), path
    for url in site_links:
        assert _site_page(url).is_file(), url
