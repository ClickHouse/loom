"""PreToolUse hook for the loom-reviewer subagent: run only commands on a short read-only list.

Reads the hook payload on stdin. Exit 0 allows the command; exit 2 blocks it and the
message on stderr goes back to the agent; any error blocks too. The reviewer reads
files with its Read, Grep and Glob tools, so Bash is kept to git and gh reads, the
prose checker and the unit tests, run from the project directory. Running the unit
tests executes the PR's own test code, so review in Bash only a PR you would run
locally.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

COMMANDS = (
    ("git", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("git", "status"),
    ("git", "rev-parse"),
    ("git", "merge-base"),
    ("git", "ls-files"),
    ("git", "blame"),
    ("gh", "pr", "view"),
    ("gh", "pr", "diff"),
    ("gh", "pr", "checks"),
    ("gh", "run", "view"),
    ("gh", "run", "list"),
    ("gh", "issue", "view"),
    ("gh", "issue", "list"),
    ("python3", ".claude/skills/loom-writing/scripts/check_prose.py"),
)
# Allowed only after a pipe, reading the previous command's output.
FILTERS = {"head", "tail", "wc", "grep"}
GIT_WRITE_FLAGS = ("--output", "--ext-diff", "--textconv")
# tail and head read the pipe: a line count, nothing to follow.
LINE_COUNT = re.compile(r"(-n ?\d+|-\d+|-q)( (-n ?\d+|-\d+|-q))*")
# Everything a listed command needs. Leaving out newline, #, ;, $, *, braces,
# parentheses, backslash and backticks rules out a second command and every expansion
# that could hide a flag; brackets are allowed only in a pytest node id.
SAFE = re.compile(r"[\w .,/:=@+^~%'\"|&\[\]-]*")
PYTEST = (".venv/bin/python", "-m", "pytest")
PYTEST_FLAG = re.compile(
    r"-q|-qq|-v|-x|-rA|--tb=\w+|-p|no:randomly|--randomly-seed=\d+|--no-header"
)
UNIT_TEST = re.compile(r"tests/unit(/[\w./-]*)?(::[\w\[\]-]+)*")


def _pytest_ok(args: list[str]) -> bool:
    # Without a path pytest collects every test directory, the integration suite too.
    unit_path = False
    after_k = False
    for arg in args:
        if after_k:  # the -k expression selects tests; it opens nothing
            after_k = False
        elif arg == "-k":
            after_k = True
        elif UNIT_TEST.fullmatch(arg) and ".." not in arg:
            unit_path = True
        elif not PYTEST_FLAG.fullmatch(arg):
            return False
    return unit_path


def _without_jq(words: list[str]) -> list[str]:
    # gh hands the --jq expression to jq; it starts with `.`, so no glob can make it a flag.
    out = []
    for i, word in enumerate(words):
        if i and words[i - 1] in {"--jq", "-q"} and words[0] == "gh":
            continue
        out.append(word)
    return out


def _segment_refusal(words: list[str], piped: bool) -> str | None:
    name = f"`{' '.join(words[:3])}`"
    if any(w.startswith("--watch") for w in words):
        return f"{name} never exits"
    if piped and words[0] in {"head", "tail"}:
        if len(words) > 1 and not LINE_COUNT.fullmatch(" ".join(words[1:])):
            return f"{name}: after a pipe, head and tail take only a line count"
        return None
    if piped:
        return None if words[0] in FILTERS else f"{name} cannot read a pipe here"
    if tuple(words[:3]) == PYTEST:
        return (
            None
            if _pytest_ok(words[3:])
            else "pytest runs only tests/unit paths with -q, -x, -k and similar flags"
        )
    if any("[" in w or "]" in w for w in _without_jq(words)):
        return "brackets are allowed only in a pytest node id or a --jq expression"
    if words[0] == "git" and any(w.startswith(GIT_WRITE_FLAGS) for w in words):
        return f"{name} with that flag writes a file or runs a program"
    if not any(tuple(words[: len(c)]) == c for c in COMMANDS):
        return f"{name} is not on the reviewer's read-only list"
    return None


def refusal(command: str) -> str | None:
    command = re.sub(r"\s2>&1\b|\s2?>\s*/dev/null\b", "", command)
    if not SAFE.fullmatch(command):
        return (
            "it has a character that could start a second command or a shell expansion"
            " (`*` is refused even quoted: use the Glob or Grep tool)"
        )
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segment: list[str] = []
    piped = False
    for token in [*lexer, "&&"]:
        if token in {"&&", "|"}:
            reason = _segment_refusal(segment, piped) if segment else f"nothing before `{token}`"
            if reason:
                return reason
            segment, piped = [], token == "|"
        elif set(token) <= set("&|;<>()"):
            return f"`{token}` is not allowed: join commands with `&&` or `|`, and redirect nothing"
        else:
            segment.append(token)
    return None


def main() -> int:
    try:
        reason = refusal(json.load(sys.stdin)["tool_input"]["command"])
    except Exception as e:  # noqa: BLE001 — a hook that cannot decide must refuse, not allow
        reason = f"the command could not be checked ({type(e).__name__})"
    if reason:
        print(f"loom-reviewer is read-only: {reason}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
