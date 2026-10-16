"""The agent instruction files stay loadable and in sync.

AGENTS.md is the one instruction file; CLAUDE.md imports it, and Codex reads the
skills through the `.agents/skills` symlink. Skills and subagents need the
frontmatter their loaders read, and a path-scoped rule whose globs match nothing
has silently stopped loading. Links are checked by test_doc_links.py.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Tracked skills only: `npx skills add` may install an ignored skill beside them.
SKILLS = sorted(
    ROOT / p
    for p in subprocess.run(
        ["git", "ls-files", ".claude/skills"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    if p.endswith("/SKILL.md")
)
RULES = sorted((ROOT / ".claude" / "rules").rglob("*.md"))
AGENTS = sorted((ROOT / ".claude" / "agents").glob("*.md"))
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# The open skill format caps a description at 1024 characters.
MAX_DESCRIPTION = 1024
MAX_SKILL_LINES = 500


def _frontmatter(path: Path) -> tuple[dict[str, str | list[str]], str]:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, f"{path.relative_to(ROOT)}: no frontmatter"
    meta: dict[str, str | list[str]] = {}
    key = ""
    for line in m.group(1).splitlines():
        if item := re.match(r"^\s+-\s+(.*)$", line):
            value = meta.setdefault(key, [])
            assert isinstance(value, list), f"{path}: {key} mixes a scalar and a list"
            value.append(item.group(1).strip().strip("\"'"))
        elif pair := re.match(r"^([A-Za-z_-]+):\s*(.*)$", line):
            key, value = pair.group(1), pair.group(2).strip()
            if value:
                meta[key] = value.strip("\"'")
    return meta, text[m.end() :]


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return set(out.stdout.splitlines())


def test_claude_md_imports_agents_md() -> None:
    first = (ROOT / "CLAUDE.md").read_text().splitlines()[0]
    assert first == "@AGENTS.md"


def test_codex_reads_the_same_skills() -> None:
    link = ROOT / ".agents" / "skills"
    assert link.is_symlink() and link.resolve() == (ROOT / ".claude" / "skills").resolve()


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(skill: Path) -> None:
    meta, body = _frontmatter(skill)
    assert meta.get("name") == skill.parent.name
    assert NAME.match(skill.parent.name) and len(skill.parent.name) <= 64
    description = meta.get("description")
    assert isinstance(description, str) and 0 < len(description) <= MAX_DESCRIPTION
    assert body.count("\n") <= MAX_SKILL_LINES


def test_agents_md_lists_every_skill() -> None:
    text = (ROOT / "AGENTS.md").read_text()
    listed = set(re.findall(r"\.claude/skills/(loom-[a-z0-9-]+)/", text))
    assert listed == {s.parent.name for s in SKILLS}


@pytest.mark.parametrize("agent", AGENTS, ids=lambda p: p.stem)
def test_subagent_frontmatter(agent: Path) -> None:
    meta, _ = _frontmatter(agent)
    assert meta.get("name") == agent.stem
    assert meta.get("description")
    for skill in meta.get("skills", []):
        assert (ROOT / ".claude" / "skills" / skill / "SKILL.md").is_file(), skill


@pytest.mark.parametrize("rule", RULES, ids=lambda p: p.stem)
def test_rule_paths_match_tracked_files(rule: Path) -> None:
    meta, _ = _frontmatter(rule)
    globs = meta.get("paths")
    assert isinstance(globs, list) and globs, "a rule without paths loads in every session"
    tracked = _tracked()
    for glob in globs:
        pattern = (
            re.escape(glob)
            .replace(r"\*\*/", "(.*/)?")
            .replace(r"\*\*", ".*")
            .replace(r"\*", "[^/]*")
        )
        assert any(re.fullmatch(pattern, p) for p in tracked), f"{glob} matches no tracked file"


def _matches(rule: str, command: str) -> bool:
    """Claude Code's Bash rule match: `:*` is a word-boundary prefix, `*` any text."""
    command = re.sub(
        r"^(?:[A-Z_][A-Z0-9_]*=\S* )+", "", command
    )  # deny rules strip env assignments
    pattern = rule.removeprefix("Bash(").removesuffix(")")
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + "*"
        if command == pattern[:-1].rstrip():
            return True
        pattern = pattern[:-1] + " *"
    return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), command) is not None


DENIED = (
    "git stash",
    "git stash pop",
    "git push --force",
    "git push -f origin br",
    "git push origin br --force",
    "git push origin +br",
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin HEAD:refs/heads/main",
    "git push origin v1.2.3",
    "git push --tags",
    "gh workflow run release.yml --ref v1.2.3",
    "gh release create v1.2.3",
    "./redeploy.sh",
    "bash redeploy.sh --rollback",
    "./redeploy.sh --force",
    "bash ./redeploy.sh",
    "/opt/x/redeploy.sh --force",
    "git push origin refs/tags/v1.2.3",
    "git push origin HEAD --follow-tags",
    "git push origin main -u",
    "LOOM_COLOUR=blue docker compose -f dev/docker-compose.colour.yml up -d",
    "docker rm -f loom-blue",
    "docker stop loom-workers",
)
NOT_DENIED = (
    "git push -u origin HEAD",
    "git push --force-with-lease",
    "git push origin br --force-with-lease",
    "./redeploy.sh --status",
    "git push -u origin validate-settings",
    "LOOM_COLOUR=blue docker compose -f dev/docker-compose.colour.yml config -q",
    "docker rm -f ch-my-branch",
)


def test_settings_deny_shared_and_production_commands() -> None:
    perms = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]
    for command in DENIED:
        assert any(_matches(r, command) for r in perms["deny"]), command
    for command in NOT_DENIED:
        assert not any(_matches(r, command) for r in perms["deny"]), command
    assert _matches(
        "Bash(.venv/bin/python -m pytest tests/unit/*)",
        ".venv/bin/python -m pytest tests/unit/test_x.py",
    )
