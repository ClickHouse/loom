"""mentor.guide handler: the agent guide in skills/loom/, read once per process."""

from __future__ import annotations

import functools

import yaml

from server.core.paths import AGENT_GUIDE_DIR
from server.insights.api.models_mentor import MentorGuideInput, MentorGuideOutput
from server.memory.domain.okf_bundle import split_frontmatter


@functools.cache
def _guide() -> tuple[str, str, str]:
    """(version, skill body, reference) from the shipped files."""
    front, body, _ = split_frontmatter((AGENT_GUIDE_DIR / "SKILL.md").read_text(encoding="utf-8"))
    version = str(yaml.safe_load(front)["metadata"]["version"])
    reference = (AGENT_GUIDE_DIR / "reference.md").read_text(encoding="utf-8")
    return version, body.lstrip("\n"), reference


async def handle_mentor_guide(inp: MentorGuideInput) -> MentorGuideOutput:
    version, skill, reference = _guide()
    text = {"skill": skill, "reference": reference, "all": skill + "\n" + reference}[inp.part]
    return MentorGuideOutput(version=version, part=inp.part, text=text)
