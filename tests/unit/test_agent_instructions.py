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
import sys
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
# Codex lists every installed skill's description within about 8000 characters and
# shortens or drops the rest; leave room for the user's own skills.
MAX_DESCRIPTIONS_TOTAL = 6000
MIN_TRIGGER_CASES = 3


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


def test_descriptions_fit_the_codex_listing() -> None:
    total = sum(len(str(_frontmatter(s)[0]["description"])) for s in SKILLS)
    assert total <= MAX_DESCRIPTIONS_TOTAL, total


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_has_trigger_cases(skill: Path) -> None:
    cases = json.loads((skill.parent / "evals" / "triggers.json").read_text())
    assert all(set(c) == {"query", "should_trigger"} for c in cases)
    assert sum(c["should_trigger"] for c in cases) >= MIN_TRIGGER_CASES
    assert sum(not c["should_trigger"] for c in cases) >= MIN_TRIGGER_CASES


def test_each_trigger_case_belongs_to_one_skill() -> None:
    owners: dict[str, str] = {}
    for skill in SKILLS:
        for case in json.loads((skill.parent / "evals" / "triggers.json").read_text()):
            if case["should_trigger"]:
                query = case["query"].lower()
                assert query not in owners, f"{skill.parent.name} and {owners[query]}: {query}"
                owners[query] = skill.parent.name


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_manual_skills_are_manual_in_codex_too(skill: Path) -> None:
    meta, _ = _frontmatter(skill)
    codex = skill.parent / "agents" / "openai.yaml"
    manual = meta.get("disable-model-invocation") == "true"
    assert manual == (codex.is_file() and "allow_implicit_invocation: false" in codex.read_text())


def _check_prose(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    script = ROOT / ".claude" / "skills" / "loom-writing" / "scripts" / "check_prose.py"
    return subprocess.run(
        [sys.executable, str(script), *args], input=stdin, capture_output=True, text=True
    )


def test_check_prose_flags_filler_and_internal_details() -> None:
    text = "We leverage a robust cache.\nLogs are under /home/alice/loom.\n`leverage` in code\n"
    out = _check_prose("-", stdin=text)
    assert out.returncode == 1
    assert out.stdout.splitlines() == [
        "-:1: leverage -> use",
        "-:1: robust -> the property itself (retries twice, fails closed)",
        "-:2: /home/alice -> remove a home directory path",
    ]
    assert _check_prose("-", stdin="Search keeps pinned rows.\n").returncode == 0


def test_agent_instructions_pass_their_own_prose_check() -> None:
    files = [ROOT / "AGENTS.md", ROOT / "CLAUDE.md", *RULES, *AGENTS]
    files += [s for s in SKILLS if s.parent.name != "loom-writing"]  # it lists the words
    out = _check_prose(*map(str, files))
    assert out.returncode == 0, out.stdout


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
    "git push origin HEAD:heads/main",
    "git push --mirror origin",
    "git push origin --all",
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
