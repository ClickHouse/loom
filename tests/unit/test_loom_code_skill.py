"""The loom-code Agent Skill must name only operations that exist — the
skill is read by agents on other tools, so a renamed op would send them
to a 404 with no hook to correct it."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "loom-code" / "SKILL.md"


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md needs YAML frontmatter"
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def test_skill_frontmatter_follows_agent_skills_shape() -> None:
    fm = _frontmatter(SKILL.read_text())
    assert fm["name"] == "loom-code"
    assert "WHEN:" in fm["description"], (
        "description carries the trigger (WHEN:) like the MCP tool prose"
    )
    assert len(fm["description"]) <= 1024


def test_skill_names_only_registered_code_operations() -> None:
    from server.operations.registry import ALL_OPERATIONS

    registered = {op.name for op in ALL_OPERATIONS}
    named = set(re.findall(r"`?(code\.[a-z_]+)`?", SKILL.read_text()))
    unknown = sorted(n for n in named if n not in registered)
    assert not unknown, f"skill names unregistered ops: {unknown}"
    # the route-then-read core must be present
    assert {"code.search", "code.symbol", "code.callers", "code.outcome"} <= named


def test_marketplace_lists_the_skill_plugin_with_repo_root_source() -> None:
    mp = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in mp["plugins"] if p["name"] == "loom-code-skill")
    assert entry["source"] == "./"
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "loom-code-skill"
    # one version, stated twice (ai-ready enforces the same in its CI)
    m = re.search(r'^\s+version:\s*"([^"]+)"', SKILL.read_text(), re.M)
    assert m and m.group(1) == manifest["version"]
