"""mentor.guide handler: the agent guide in skills/loom/, read once per process."""

from __future__ import annotations

import functools
import re

from server.core.paths import AGENT_GUIDE_DIR
from server.insights.api.models_mentor import MentorGuideInput, MentorGuideOutput

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n+", re.S)
_VERSION = re.compile(r'^\s+version:\s*"?([^"\n]+)"?\s*$', re.M)


@functools.cache
def _guide() -> tuple[str, str, str]:
    """(version, skill body, reference) from the shipped files."""
    skill = (AGENT_GUIDE_DIR / "SKILL.md").read_text(encoding="utf-8")
    reference = (AGENT_GUIDE_DIR / "reference.md").read_text(encoding="utf-8")
    front = _FRONTMATTER.match(skill)
    version = _VERSION.search(front.group(1)) if front else None
    if front is None or version is None:
        raise RuntimeError("skills/loom/SKILL.md has no frontmatter with metadata.version")
    return (version.group(1), skill[front.end() :], reference)


async def handle_mentor_guide(inp: MentorGuideInput) -> MentorGuideOutput:
    version, skill, reference = _guide()
    text = {"skill": skill, "reference": reference, "all": skill + "\n" + reference}[inp.part]
    return MentorGuideOutput(version=version, part=inp.part, text=text)
