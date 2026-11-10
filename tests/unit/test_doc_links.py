"""Every relative link in the Markdown docs resolves, anchors included.

Covers the repo-root docs, docs/, the integration READMEs and the agent skills, rules
and subagents under .claude/. External URLs are not fetched. An anchor is checked
against the GitHub slugs of the target's headings.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)


def _slug(heading: str) -> str:
    text = re.sub(r"<[^>]+>", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for h in HEADING.findall(FENCE.sub("", path.read_text())):
        slug = _slug(h)
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def _links(path: Path) -> list[str]:
    return LINK.findall(FENCE.sub("", path.read_text()))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(doc: Path) -> None:
    broken = []
    for target in _links(doc):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("//"):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (doc.parent / file_part).resolve() if file_part else doc
        if not dest.exists():
            broken.append(target)
        elif anchor and dest.suffix == ".md" and anchor.lower() not in _anchors(dest):
            broken.append(target)
    assert not broken, broken
