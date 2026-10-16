"""PreToolUse hook for the loom-reviewer subagent: run only commands on a short read-only list.

Reads the hook payload on stdin. Exit 0 allows the command; exit 2 blocks it and the
message on stderr goes back to the agent; any error blocks too. The reviewer reads
files with its Read, Grep and Glob tools, so Bash is kept to git and gh reads, the
prose checker and the unit tests. Running the unit tests executes the PR's own test
code, so review in Bash only a PR you would run locally.
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
    ("cd",),
)
# Allowed only after a pipe, reading the previous command's output.
FILTERS = {"head", "tail", "wc", "grep"}
GIT_WRITE_FLAGS = ("--output", "--ext-diff", "--textconv")
PYTEST = (".venv/bin/python", "-m", "pytest")
PYTEST_FLAG = re.compile(
    r"-q|-qq|-v|-x|-rA|--tb=\w+|-p|no:randomly|--randomly-seed=\d+|--no-header"
)
UNIT_TEST = re.compile(r"tests/unit(/[\w./-]*)?(::[\w\[\]-]+)*")


def _pytest_ok(args: list[str]) -> bool:
    after_k = False
    for arg in args:
        if after_k:  # the -k expression selects tests; it opens nothing
            after_k = False
        elif arg == "-k":
            after_k = True
        elif ".." in arg or not (PYTEST_FLAG.fullmatch(arg) or UNIT_TEST.fullmatch(arg)):
            return False
    return True


def _segment_ok(words: list[str], piped: bool) -> bool:
    if piped:
        return words[0] in FILTERS
    if words[0] == "LOOM_ENV_FILE=":  # the empty value: load no .env
        words = words[1:]
    if tuple(words[:3]) == PYTEST:
        return _pytest_ok(words[3:])
    if words[0] == "git" and any(w.startswith(GIT_WRITE_FLAGS) for w in words):
        return False
    return any(tuple(words[: len(c)]) == c for c in COMMANDS)


def refusal(command: str) -> str | None:
    if "`" in command or re.search(r"[$<>]\(", command):
        return "command and process substitution can run anything"
    command = re.sub(r"\s2>&1\b|\s2?>\s*/dev/null\b", "", command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segment: list[str] = []
    piped = False
    for token in [*lexer, "&&"]:
        if token in {"&&", "|"}:
            if not segment or not _segment_ok(segment, piped):
                return f"`{' '.join(segment[:4])}` is not on the reviewer's read-only list"
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
