"""Flag filler words, internal details and secrets in text an agent is about to commit or post.

Usage: check_prose.py FILE... (or - for stdin). Prints path:line: phrase -> what to do.
Exit 0 when clean, 1 on any hit, 2 when a file cannot be read. Filler is checked in
prose only; internal details and secrets are checked everywhere, code blocks included,
because pasted logs are where they leak. A secret is printed as its first four
characters only.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from scripts.gates.check_no_secrets import PROVIDER_KEY_PATTERNS  # noqa: E402 — the repo's gate

# The phrases from SKILL.md's table that are filler in every use; the other rows
# need a reader's judgement.
FILLER = {
    r"leverag(e|es|ed|ing)": "use",
    r"utili[sz](e|es|ed|ing)": "use",
    r"robust(ly|ness)?": "the property itself (retries twice, fails closed)",
    r"comprehensive(ly)?": "what it covers (every read op, all five planes)",
    r"seamless(ly)?": "delete, or the mechanism",
    r"streamlin(e|es|ed|ing)": "what was removed",
    r"delv(e|es|ed|ing)": "read, check",
    r"dive into": "read, check",
    r"it(['’]s| is) worth noting( that)?": "delete; state the point",
    r"importantly": "delete; state the point",
    r"in order to": "to",
    r"a (wide )?variety of": "the count, or the list",
    r"facilitat(e|es|ed|ing)": "the verb for what happens",
    r"empower(s|ed|ing)?": "the verb for what happens",
    r"going forward|at this point in time": "delete",
    r"holistic(ally)?": "delete",
    r"cutting[- ]edge|state[- ]of[- ]the[- ]art|best[- ]in[- ]class": "delete",
    r"game[- ]chang(er|ing)": "delete",
    r"great (catch|question|point)": "delete; state what changed",
    r"happy to help|hope this helps|let me know if": "delete",
    r"i apologi[sz]e|sorry for the confusion": "delete; state the correction",
}

# Details that never belong in public text. A path counts only where a word does not
# run into it, so `https://example.com/home/page` is not one.
INTERNAL = {
    r"(?<![\w.])/(home|Users)/[\w.-]+|(?<![\w.])/root/": "a home directory path",
    r"(?<![\w.])/opt/[\w.-]+": "a host path",
    r"\b(10\.\d+|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+\b": "a private IP address",
    r"\b[\w.-]+\.(internal|corp|lan)\b": "an internal host name",
}
# The gate leaves these two out: a GitHub token's body is all word characters, so its
# fake-value rule never fires on one, and the repo's JWT and token test fixtures would
# fail it.
SECRETS = [
    ("JWT", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}")),
    ("GitHub token", re.compile(r"\b(?:gh[oprsu]_\w{20,}|github_pat_\w{20,})")),
    *PROVIDER_KEY_PATTERNS,
]

_FILLER = [
    (re.compile(rf"\b(?:{pattern})(?!\w)", re.IGNORECASE), f"-> {fix}")
    for pattern, fix in FILLER.items()
]
_INTERNAL = [(re.compile(pattern), f"-> remove {what}") for pattern, what in INTERNAL.items()]
_INLINE_CODE = re.compile(r"`[^`]*`")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def split_fences(text: str) -> Iterator[tuple[int, str, bool]]:
    """Each line with its number and whether it is code: a fence line or inside a fence.

    A fence closes on a bare run of the same character at least as long as the one
    that opened it, so a ``` inside a ```` block, or a ```python line, does not; a
    backtick run followed by another backtick is inline code, not a fence.
    """
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        marker = _FENCE.match(line)
        run = marker.group(1) if marker else ""
        if run and not fence and not (run[0] == "`" and "`" in line.strip()[len(run) :]):
            fence = run
            yield number, line, True
        elif run and fence and line.strip() == run and run.startswith(fence):
            fence = ""
            yield number, line, True
        else:
            yield number, line, bool(fence)


def check(text: str) -> list[tuple[int, str, str]]:
    hits = []
    for number, line, in_code in split_fences(text):
        for pattern, advice in _INTERNAL:
            hits += [(number, m.group(0), advice) for m in pattern.finditer(line)]
        for label, pattern in SECRETS:
            hits += [
                (number, m.group(0)[:4] + "…", f"-> remove the {label}")
                for m in pattern.finditer(line)
            ]
        if not in_code:
            prose = _INLINE_CODE.sub("", line)
            for pattern, advice in _FILLER:
                hits += [(number, m.group(0), advice) for m in pattern.finditer(prose)]
    return sorted(hits)


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    status = 0
    for path in paths:
        try:
            text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"{path}: cannot read: {e}", file=sys.stderr)
            status = 2
            continue
        for number, phrase, advice in check(text):
            print(f"{path}:{number}: {phrase} {advice}")
            status = max(status, 1)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
