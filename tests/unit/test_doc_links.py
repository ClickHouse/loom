"""Every relative link in the Markdown docs resolves, anchors included.

Covers the repo-root docs, docs/, the integration and SDK READMEs and the agent skills,
rules and subagents under .claude/. External URLs are not fetched. An anchor is checked
against the GitHub slugs of the target's headings.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.markdown_links import anchors, links
from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]
DOCS = sorted(
    {
        *ROOT.glob("*.md"),
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "integrations").glob("*/README.md"),
        ROOT / "integrations" / "README.md",
        ROOT / "sdk" / "python" / "README.md",
        ROOT / "sdk" / "typescript" / "README.md",
        *(ROOT / p for p in tracked(".claude/skills") if p.endswith(".md")),
        *(ROOT / p for p in tracked(".claude/rules", ".claude/agents") if p.endswith(".md")),
    }
)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(doc: Path) -> None:
    broken = []
    for target in links(doc):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("//"):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (doc.parent / file_part).resolve() if file_part else doc
        if not dest.exists():
            broken.append(target)
        elif anchor and dest.suffix == ".md" and anchor.lower() not in anchors(dest):
            broken.append(target)
    assert not broken, broken
