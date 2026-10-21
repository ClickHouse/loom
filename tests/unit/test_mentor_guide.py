"""mentor.guide serves skills/loom/ as shipped, and every op the guide names exists."""

from __future__ import annotations

import asyncio
import json
import re

import server
from server.core.paths import AGENT_GUIDE_DIR, REPO_ROOT
from server.insights.api.models_mentor import MentorGuideInput
from server.insights.app.mentor_guide import handle_mentor_guide
from server.operations.registry import OPERATIONS_BY_NAME


def _guide(part: str):
    return asyncio.run(handle_mentor_guide(MentorGuideInput(part=part)))


def test_skill_part_is_the_body_without_frontmatter_and_carries_the_version() -> None:
    out = _guide("skill")
    # The root skills ship in one plugin and are released with the server, at its version.
    manifest = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert out.version == manifest["version"] == server.__version__
    assert out.text.startswith("# loom")
    assert "description:" not in out.text


def test_all_is_the_skill_followed_by_the_reference() -> None:
    skill, reference, both = _guide("skill").text, _guide("reference").text, _guide("all").text
    assert both == skill + "\n" + reference
    assert reference == (AGENT_GUIDE_DIR / "reference.md").read_text(encoding="utf-8")


def test_every_op_the_guide_names_is_registered() -> None:
    text = _guide("all").text
    groups = {name.split(".")[0] for name in OPERATIONS_BY_NAME}
    named = {
        m
        for m in re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)`", text)
        if m.split(".")[0] in groups and not m.endswith((".md", ".py"))
    }
    assert named, "the guide names no op"
    assert named <= set(OPERATIONS_BY_NAME), sorted(named - set(OPERATIONS_BY_NAME))


def test_experimental_ops_in_the_reference_carry_the_marker() -> None:
    for row in re.findall(r"^\| (`[^|]+)\|", _guide("reference").text, re.M):
        for name, mark in re.findall(r"`([a-z_.]+)`(†?)", row):
            if name in OPERATIONS_BY_NAME:
                experimental = OPERATIONS_BY_NAME[name].maturity == "experimental"
                assert bool(mark) == experimental, name


def test_skill_frontmatter_follows_agent_skills_shape() -> None:
    front = re.match(r"\A---\n(.*?)\n---\n", (AGENT_GUIDE_DIR / "SKILL.md").read_text(), re.S)
    assert front, "SKILL.md needs YAML frontmatter"
    assert re.search(r"^name: loom$", front.group(1), re.M)
    description = re.search(r'^description: "(.*)"$', front.group(1), re.M)
    assert description and "WHEN:" in description.group(1)
    assert len(description.group(1)) <= 1024
