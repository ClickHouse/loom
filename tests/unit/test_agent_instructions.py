"""The agent instruction files stay loadable, within budget and true to the repository.

AGENTS.md is the one instruction file; CLAUDE.md imports it, and Codex reads each skill
through its symlink under `.agents/skills/`. A skill, rule or subagent whose
frontmatter does not parse is loaded without it; a rule whose globs match nothing has
silently stopped loading; a path, make target or module these files name that no
longer exists sends every agent down the wrong road. When this test fails after a
rename, it names the file that still says the old name. Links are checked by
test_doc_links.py. The trigger cases are run by the skill-creator description
optimizer, not by CI; this test keeps their shape.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from server.code.app.index.instructions_lint import lint_text
from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]
TRACKED = set(tracked())
# Tracked skills only: `npx skills add` may install an ignored skill beside them.
SKILLS = sorted(ROOT / p for p in TRACKED if re.fullmatch(r"\.claude/skills/[^/]+/SKILL\.md", p))
RULES = sorted(ROOT / p for p in tracked(".claude/rules") if p.endswith(".md"))
AGENTS = sorted(ROOT / p for p in tracked(".claude/agents") if p.endswith(".md"))
INSTRUCTIONS = [ROOT / "AGENTS.md", ROOT / "CLAUDE.md", *RULES, *AGENTS, *SKILLS]
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# The open skill format caps a description at 1024 characters.
MAX_DESCRIPTION = 1024
MAX_SKILL_LINES = 500
# Every session pays for the skill descriptions; Codex lists all installed skills
# within about 8000 characters and shortens or drops the rest.
MAX_DESCRIPTIONS_TOTAL = 4000
# AGENTS.md loads in every session of every agent.
MAX_AGENTS_MD_BYTES = 9000
MIN_TRIGGER_CASES = 3
HOOK = ROOT / ".claude" / "hooks" / "read_only_bash.py"
CHECK_PROSE = ROOT / ".claude" / "skills" / "loom-writing" / "scripts" / "check_prose.py"
_spec = importlib.util.spec_from_file_location("check_prose", CHECK_PROSE)
check_prose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_prose)


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, f"{path.relative_to(ROOT)}: no frontmatter"
    meta = yaml.safe_load(m.group(1))
    assert isinstance(meta, dict), f"{path.relative_to(ROOT)}: frontmatter is not a mapping"
    return meta, text[m.end() :]


def _code(path: Path) -> list[str]:
    """Inline code spans from prose lines, and every line inside a fenced block."""
    code = []
    for _, line, in_code in check_prose.split_fences(path.read_text()):
        code += [line] if in_code else re.findall(r"`([^`\n]+)`", line)
    return code


def test_claude_md_imports_agents_md() -> None:
    first = (ROOT / "CLAUDE.md").read_text().splitlines()[0]
    assert first == "@AGENTS.md"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_codex_reads_each_skill_through_its_link(skill: Path) -> None:
    link = ROOT / ".agents" / "skills" / skill.parent.name
    assert link.is_symlink() and link.resolve() == skill.parent.resolve()
    assert f".agents/skills/{skill.parent.name}" in TRACKED


def test_every_loom_skill_is_tracked() -> None:
    # .gitignore names each skill; one left out is silently missing from the commit.
    on_disk = {
        p.parent.parent.name for p in (ROOT / ".claude" / "skills").glob("*/evals/triggers.json")
    }
    assert on_disk == {s.parent.name for s in SKILLS}


def test_always_loaded_text_fits_its_budget() -> None:
    assert len((ROOT / "AGENTS.md").read_bytes()) <= MAX_AGENTS_MD_BYTES
    total = sum(len(_frontmatter(s)[0]["description"]) for s in SKILLS)
    assert total <= MAX_DESCRIPTIONS_TOTAL, total


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(skill: Path) -> None:
    meta, body = _frontmatter(skill)
    assert meta.get("name") == skill.parent.name
    assert NAME.match(skill.parent.name) and len(skill.parent.name) <= 64
    description = meta.get("description")
    assert isinstance(description, str) and 0 < len(description) <= MAX_DESCRIPTION
    assert body.count("\n") <= MAX_SKILL_LINES


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_has_trigger_cases(skill: Path) -> None:
    cases = json.loads((skill.parent / "evals" / "triggers.json").read_text())
    assert all(set(c) == {"query", "should_trigger"} for c in cases)
    positives = sum(c["should_trigger"] for c in cases)
    if _frontmatter(skill)[0].get("disable-model-invocation"):
        assert positives == 0, "a manual-only skill never loads on its own"
    else:
        assert positives >= MIN_TRIGGER_CASES
    assert len(cases) - positives >= MIN_TRIGGER_CASES


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
    policy = yaml.safe_load(codex.read_text())["policy"] if codex.is_file() else {}
    assert bool(meta.get("disable-model-invocation")) == (
        policy.get("allow_implicit_invocation") is False
    )


def test_agents_md_lists_every_rule() -> None:
    listed = set(re.findall(r"^\| `([a-z-]+)` \|", (ROOT / "AGENTS.md").read_text(), re.MULTILINE))
    assert listed == {r.stem for r in RULES}


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
    for glob in globs:
        pattern = (
            re.escape(glob)
            .replace(r"\*\*/", "(.*/)?")
            .replace(r"\*\*", ".*")
            .replace(r"\*", "[^/]*")
        )
        assert any(re.fullmatch(pattern, p) for p in TRACKED), f"{glob} matches no tracked file"


@pytest.mark.parametrize("doc", INSTRUCTIONS, ids=lambda p: str(p.relative_to(ROOT)))
def test_named_paths_exist(doc: Path) -> None:
    relative = str(doc.relative_to(ROOT))
    stale = [f.ref.value for f in lint_text(relative, doc.read_text(), TRACKED).stale]
    assert not stale, f"{relative} names paths that do not exist: {stale}"


@pytest.mark.parametrize("doc", INSTRUCTIONS, ids=lambda p: str(p.relative_to(ROOT)))
def test_named_make_targets_and_modules_exist(doc: Path) -> None:
    text = doc.read_text()
    code = _code(doc)
    targets = set(re.findall(r"^([a-z][\w-]*):", (ROOT / "Makefile").read_text(), re.MULTILINE))
    named = {
        target
        for snippet in code
        for line in re.findall(r"\bmake ((?:[a-z][\w-]*[a-z0-9](?: |$))+)", snippet, re.MULTILINE)
        for target in line.split()
    }
    assert named <= targets, f"{doc.relative_to(ROOT)}: no such make target {named - targets}"
    for module in re.findall(r"-m ((?:scripts|tests|server)\.[\w.]+)", text):
        base = ROOT / module.replace(".", "/")
        assert base.with_suffix(".py").is_file() or (base / "__init__.py").is_file(), module


def _matches(rule: str, command: str) -> bool:
    """This test's model of Claude Code's Bash rule match: `:*` and `*` are any text.

    It proves the rules match the commands below under that model; Claude Code also
    splits compound commands and strips wrappers, which the model does not.
    """
    command = re.sub(r"^(?:[A-Z_][A-Z0-9_]*=\S* )+", "", command)  # deny rules skip env assignments
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
    "git push origin refs/tags/v1.2.3",
    "git push origin HEAD --follow-tags",
    "git push origin main -u",
    "git push origin 'HEAD:main'",
    "git push origin HEAD:main --no-verify",
    "git push origin HEAD:main -u",
    "git push origin HEAD:main --force-with-lease",
    "git --no-pager push origin main",
    "git -P push -f origin x",
    "git clean -xdf",
    "git clean -df",
    "git push -uf origin br",
    "git push origin br -fu",
    "docker-compose -f dev/docker-compose.colour.yml down",
    "git -C ../x push --force origin br",
    "git -c push.default=current push -f",
    "git diff --output=/tmp/x",
    "git log -p --output=/tmp/x",
    "git fetch --upload-pack='touch x' origin",
    "git reset --hard origin/main",
    "git clean -fdx",
    "git checkout main",
    "git switch main",
    ".venv/bin/python -m pytest tests/unit --basetemp=/home",
    "gh workflow run release.yml --ref v1.2.3",
    "gh release create v1.2.3",
    "./redeploy.sh",
    "bash redeploy.sh --rollback",
    "./redeploy.sh --force",
    "bash ./redeploy.sh",
    "/opt/x/redeploy.sh --force",
    "./redeploy.sh 2>&1",
    "sudo ./redeploy.sh",
    "LOOM_COLOUR=blue docker compose -f dev/docker-compose.colour.yml up -d",
    "docker compose -f ./dev/docker-compose.colour.yml down",
    "docker rm -f loom-blue",
    "docker stop loom-workers",
)
ASKED = (
    "gh pr create --base main --title t --body-file b.md",
    "gh pr merge 57 --squash",
    "gh pr comment 57 --body x",
    "gh issue create --title t --body-file b.md",
    "gh api repos/ClickHouse/loom/pulls/57/comments/1/replies -f body=x",
    "make bench-quick",
    ".venv/bin/python -m bench.rejudge --hyp x",
    "git push --force-with-lease",
    "git push origin br --force-with-lease",
    "PYTHONPATH=x .venv/bin/python bench/longmemeval.py --repeats 3",
    ".venv/bin/python ./bench/locomo_qa.py",
    "/w/.venv/bin/python /w/bench/locomo_qa.py --repeats 3",
    "python3 bench/locomo_qa.py",
    "python3 ./bench/locomo_qa.py",
    "python3 /w/bench/locomo_qa.py --repeats 3",
)
NOT_DENIED = (
    "git push -u origin HEAD",
    "git push --force-with-lease",
    "git push -u origin v2-search-floor",
    "./redeploy.sh --status",
    "git push -u origin validate-settings",
    "git diff origin/main...HEAD",
    "git push -u origin feature-flags",
    "LOOM_COLOUR=blue docker compose -f dev/docker-compose.colour.yml config -q",
    "docker rm -f ch-my-branch",
    "git show abc:redeploy.sh",
    "git diff origin/main...HEAD -- redeploy.sh",
    "git log -p -- ./redeploy.sh",
    "cat ./redeploy.sh",
    "bash redeploy.sh --status",
    "sh redeploy.sh --status",
)
# Reads of files whose execution asks first: each would stop a background reviewer on a prompt.
NOT_ASKED = (
    "git show abc:bench/locomo_qa.py",
    "git diff origin/main...HEAD -- bench/longmemeval.py",
    "git log --grep=python -- bench/locomo_qa.py",
)


def test_settings_deny_shared_and_production_commands() -> None:
    perms = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]
    for command in DENIED:
        assert any(_matches(r, command) for r in perms["deny"]), command
    for command in ASKED:  # an ask rule wins over an allow rule
        assert any(_matches(r, command) for r in perms["ask"]), command
    for command in NOT_DENIED:
        assert not any(_matches(r, command) for r in perms["deny"]), command
    for command in NOT_ASKED:
        assert not any(_matches(r, command) for r in perms["ask"]), command
    # The reviewer runs in the background; anything it runs that is not pre-approved prompts.
    reviewer = (ROOT / ".claude" / "agents" / "loom-reviewer.md").read_text()
    for command in re.findall(r"`(\.venv/bin/python -m pytest[^`]*)`", reviewer):
        assert any(_matches(r, command) for r in perms["allow"]), command
    # Only the whole unit suite runs without asking: a path after it could be anything.
    assert not any(
        _matches(r, ".venv/bin/python -m pytest tests/unit/x.py tests/integration")
        for r in perms["allow"]
    )


def _hook(command: str) -> int:
    payload = (
        command if command.startswith("{") else json.dumps({"tool_input": {"command": command}})
    )
    return subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True
    ).returncode


@pytest.mark.parametrize(
    "command",
    [
        "git diff origin/main...HEAD",
        "git log --format=%h -5 && git status",
        "git show HEAD:AGENTS.md | head -20",
        "git log -p | grep -n foo",
        "gh pr view 57 --json body --jq .body",
        ".venv/bin/python -m pytest tests/unit/test_x.py -q 2>&1 | tail -5",
        ".venv/bin/python -m pytest tests/unit -q -k 'a and not b'",
        ".venv/bin/python -m pytest 'tests/unit/test_a.py::test_b[x-1]' -q",
        "git log | grep -F x",
        "gh pr view 57 --json files --jq '.files[].path'",
        "gh pr view 57 --json files --jq='.files[].path'",
        "gh pr view 57 -q=.title",
        "gh pr view 57 --json files --jq '.files[] | .path'",
        "git log | grep -E 'a|b'",
        "git log '|' grep x",  # bash hands the quoted | to git log as an argument
        "git diff --output-indicator-new=+ HEAD~1",
        "python3 .claude/skills/loom-writing/scripts/check_prose.py AGENTS.md",
    ],
)
def test_reviewer_hook_allows_reads(command: str) -> None:
    assert _hook(command) == 0


@pytest.mark.parametrize(
    "command",
    [
        "git diff --output=/tmp/x",
        "git fetch . HEAD:x",
        "echo hi > f",
        "cat a &>b",
        "echo >(touch x)",
        "sed -n '1e touch x' f",
        "uniq in out",
        "rg --pre bash x f",
        "make lint format",
        ".venv/bin/python -m ruff check --fix-only",
        ".venv/bin/python -m pytest tests/unit --junitxml=/root/x",
        ".venv/bin/python -m pytest tests/integration",
        "LOOM_ENV_FILE=/opt/x/.env .venv/bin/python -m pytest tests/unit",
        "git log\nrm -rf x",
        "git log #\ntouch x",
        "git diff {--output=/tmp/x,HEAD}",
        "git diff $'--output=/tmp/x'",
        "git diff ${X:---output=/tmp/x}",
        "cd /other/checkout && .venv/bin/python -m pytest tests/unit",
        "gh pr checks 57 --watch",
        "gh pr checks 57 --watch=true",
        "git log | tail -qf x",
        "git log | tail +1f x",
        "git log | tail --foll x",
        "gh pr view 57 --jq [-][-]web",
        "gh pr view 57 --jq=[-][-]web",
        '.venv/bin/python -m pytest tests/unit/test_cli_demo.py "|" grep --log-file=x',
        ".venv/bin/python -m pytest tests/unit/test_x.py '&&' gh pr view --junitxml=x",
        "git log -1 2>/dev/null.x",
        "true",  # the reviewer's first call: it must be refused while the hook is on
        "git status\n2>/dev/null touch x",
        "git status\n2>&1 touch x",
        "git log || git status",
        "git log & git status",
        "gh pr view 57 --jq .x [-]w",
        "git diff --output /tmp/x",
        "git log -p --ext-diff",
        "git diff *",
        "git diff [^a]x",
        ".venv/bin/python -m pytest -q",
        ".venv/bin/python -m pytest -k x",
        "git log | tail -f",
        "echo 'mutation{x}' | gh api graphql -F query=@-",
        "git diff; rm x",
        "git log | sh",
        "gh pr comment 57 -b x",
        "{not json",
    ],
)
def test_reviewer_hook_blocks_everything_else(command: str) -> None:
    assert _hook(command) == 2


def test_tracked_files_outside_git_lists_files_and_links(tmp_path: Path) -> None:
    (tmp_path / "skills" / "a").mkdir(parents=True)
    (tmp_path / "skills" / "a" / "SKILL.md").write_text("x")
    (tmp_path / "links").mkdir()
    (tmp_path / "links" / "a").symlink_to("../skills/a")
    assert tracked(root=tmp_path) == ["links/a", "skills/a/SKILL.md"]
    assert tracked("links/a", root=tmp_path) == ["links/a"]


def _check_prose(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK_PROSE), *args], input=stdin, capture_output=True, text=True
    )


def test_check_prose_flags_filler_and_internal_details() -> None:
    text = (
        "We leverage a robust cache.\n"
        "Importantly, it’s worth noting this.\n"
        "`leverage` in code\n"
        "```\n"
        "ssh ubuntu@10.1.2.3 cat /home/ubuntu/.env\n"
        "```python\n"
        "we leverage this inside the block\n"
        "```\n"
        "token sk-ant-example-abcdefghijklmnopqrstuvwxyz0123, see https://example.com/home/page\n"
        "source file:///home/alice/loom/x.py\n"
        "```bash ssh 10.0.0.5\n"
        "```\n"
        "```leverage``` is inline code, and we leverage it\n"
    )
    out = _check_prose("-", stdin=text)
    assert out.returncode == 1
    assert out.stdout.splitlines() == [
        "-:1: leverage -> use",
        "-:1: robust -> the property itself (retries twice, fails closed)",
        "-:2: Importantly -> delete; state the point",
        "-:2: it’s worth noting -> delete; state the point",
        "-:5: /home/ubuntu -> remove a home directory path",
        "-:5: 10.1.2.3 -> remove a private IP address",
        "-:9: sk-a… -> remove the Anthropic API key",
        "-:10: /home/alice -> remove a home directory path",
        "-:11: 10.0.0.5 -> remove a private IP address",
        "-:13: leverage -> use",
    ]
    assert _check_prose("-", stdin="Search keeps pinned rows.\n").returncode == 0
    assert _check_prose("/nonexistent").returncode == 2


def test_agent_instructions_pass_their_own_prose_check() -> None:
    files = [f for f in INSTRUCTIONS if f.parent.name != "loom-writing"]  # it lists the words
    out = _check_prose(*map(str, files))
    assert out.returncode == 0, out.stdout
