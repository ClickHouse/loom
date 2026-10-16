"""PreToolUse hook for the loom-reviewer subagent: allow a Bash command only if it cannot write.

Reads the hook payload on stdin. Exit 0 allows the command; exit 2 blocks it and the
message on stderr goes back to the agent. Every part of a compound command must start
with a read-only prefix, carry no flag that makes it write, and redirect nowhere but
/dev/null.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

READ_ONLY = (
    "git diff",
    "git log",
    "git show",
    "git status",
    "git rev-parse",
    "git merge-base",
    "git ls-files",
    "git grep",
    "git blame",
    "git fetch",
    "git branch --show-current",
    "gh pr view",
    "gh pr diff",
    "gh pr checks",
    "gh pr list",
    "gh run view",
    "gh run list",
    "gh issue view",
    "gh issue list",
    "gh search",
    "gh api graphql",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "ls",
    "wc",
    "find",
    "jq",
    "diff",
    "sort",
    "uniq",
    "cut",
    "tr",
    "nl",
    "sed -n",
    "echo",
    "printf",
    "pwd",
    "cd",
    "true",
    "test",
    ".venv/bin/python -m pytest",
    ".venv/bin/python -m ruff check",
    "python3 .claude/skills/loom-writing/scripts/check_prose.py",
    "make lint",
    "make typecheck",
    "make test-unit",
    "make schema-check",
)
# Flags that turn one of the commands above into a writer, by the command they belong to.
WRITE_FLAGS = {
    "": {"--output", "--upload-pack", "--basetemp", "--fix", "-X", "--method"},
    "sed": {"-i", "--in-place"},
    "sort": {"-o"},
    "find": {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    },
}
SEPARATORS = {"&&", "||", ";", "|", "|&", "&"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
ASSIGNMENT = re.compile(r"^(LOOM_\w*|PYTHONPATH)=")


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _parts(command: str) -> list[list[str]]:
    parts: list[list[str]] = []
    for line in command.splitlines():
        part: list[str] = []
        for token in _tokens(line):
            if token in SEPARATORS:
                parts.append(part)
                part = []
            else:
                part.append(token)
        parts.append(part)
    return [p for p in parts if p]


def refusal(command: str) -> str | None:
    if "`" in command or "$(" in command or "<(" in command:
        return "command substitution can run anything"
    for part in _parts(command):
        while part and ASSIGNMENT.match(part[0]):
            part = part[1:]
        for i, token in enumerate(part):
            if token in REDIRECTS:
                target = part[i + 1] if i + 1 < len(part) else ""
                if target != "/dev/null" and not (token == ">&" and target in {"1", "2"}):
                    return "redirection writes a file"
            flag = token.split("=")[0]
            if flag in WRITE_FLAGS[""] or flag in WRITE_FLAGS.get(part[0], ()):
                return f"`{token}` makes the command write"
            if part[:3] == ["gh", "api", "graphql"] and re.search(r"\bmutation\b", token):
                return f"`{token}` makes the command write"
        words = " ".join(part)
        if not any(words == p or words.startswith(p + " ") for p in READ_ONLY):
            return f"`{' '.join(part[:3])}` is not on the reviewer's read-only list"
    return None


def main() -> int:
    command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    try:
        reason = refusal(command)
    except ValueError:  # unbalanced quotes: what would run cannot be told
        reason = "the command does not parse"
    if reason:
        print(f"loom-reviewer is read-only: {reason}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
