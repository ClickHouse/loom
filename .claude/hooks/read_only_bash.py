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
GIT_WRITE_FLAGS = {"--output", "--ext-diff", "--textconv"}
# tail and head read the pipe: a line count, nothing to follow.
LINE_COUNT = re.compile(r"(-n ?\d+|-\d+|-q)( (-n ?\d+|-\d+|-q))*")
# Everything a listed command needs. Leaving out newline, #, ;, $, *, braces,
# parentheses, backslash and backticks rules out a second command and every expansion
# that could hide a flag; brackets are allowed only in a pytest node id and in a gh
# --jq expression that starts with `.`.
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
    # A --jq expression that starts with `.` can expand only to a dotfile, never a flag.
    if words[0] != "gh":
        return words
    out = []
    for i, word in enumerate(words):
        if word.startswith(("--jq=", "-q=")):
            expression = word.split("=", 1)[1]
        elif i > 0 and words[i - 1] in {"--jq", "-q"}:
            expression = word
        else:
            expression = ""
        if not expression.startswith("."):
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
        if words[0] not in FILTERS:
            return f"{name} cannot read a pipe here"
        return None
    if tuple(words[:3]) == PYTEST:
        if not _pytest_ok(words[3:]):
            return "pytest runs only tests/unit paths with -q, -x, -k and similar flags"
        return None
    if any("[" in w or "]" in w for w in _without_jq(words)):
        return (
            "brackets are allowed only in a pytest node id"
            " or in a --jq expression that starts with `.`"
        )
    if words[0] == "git":
        flag = next((w for w in words if w.split("=", 1)[0] in GIT_WRITE_FLAGS), None)
        if flag:
            return f"`{flag}` makes git write a file or run a program"
    if not any(tuple(words[: len(c)]) == c for c in COMMANDS):
        return f"{name} is not on the reviewer's read-only list"
    return None


def _segments(command: str) -> list[tuple[str, bool]] | None:
    """Split where bash would, at an unquoted `&&` or `|`; each part says if it reads a pipe.

    None when another unquoted `&` or `|` (`||`, `|&`, a lone `&`) appears.
    """
    segments: list[tuple[str, bool]] = []
    start, piped, quote, i = 0, False, "", 0
    while i < len(command):
        char = command[i]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in "&|":
            operator = command[i : i + 2] if command[i : i + 2] in {"&&", "||", "|&"} else char
            if operator not in {"&&", "|"}:
                return None
            segments.append((command[start:i], piped))
            piped = operator == "|"
            i += len(operator)
            start = i
            continue
        i += 1
    segments.append((command[start:], piped))
    return segments


def refusal(command: str) -> str | None:
    command = re.sub(r"\s(?:2>&1|2?>\s*/dev/null)(?=\s|$)", "", command)
    if not SAFE.fullmatch(command):
        if "*" in command:
            return (
                "`*` is refused even quoted: name a directory or file (`-- server/`),"
                " or find files with the Glob or Grep tool"
            )
        return "it has a character that could start a second command, a redirection or an expansion"
    segments = _segments(command)
    if segments is None:
        return "join commands only with `&&` or `|`"
    for text, piped in segments:
        words = shlex.split(text)
        if not words:
            return "an empty command between `&&` or `|`"
        reason = _segment_refusal(words, piped)
        if reason:
            return reason
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
