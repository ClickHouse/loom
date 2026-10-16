"""Flag filler words and internal details in text an agent is about to commit or post.

Usage: check_prose.py FILE... (or - for stdin). Prints path:line: phrase -> replacement
and exits 1 on any hit. Fenced code blocks and `inline code` are skipped.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# phrase (regex, case-insensitive) -> what to write instead
FILLER = {
    r"leverag(e|es|ed|ing)": "use",
    r"utiliz(e|es|ed|ing)": "use",
    r"robust(ly|ness)?": "the property itself (retries twice, fails closed)",
    r"comprehensive(ly)?": "what it covers (every read op, all five planes)",
    r"seamless(ly)?": "delete, or the mechanism",
    r"streamlin(e|es|ed|ing)": "what was removed",
    r"delv(e|es|ed|ing)": "read, check",
    r"dive into": "read, check",
    r"it'?s worth noting( that)?": "delete; state the point",
    r"importantly,": "delete; state the point",
    r"in order to": "to",
    r"a (wide )?variety of": "the count, or the list",
    r"facilitat(e|es|ed|ing)": "the verb for what happens",
    r"empower(s|ed|ing)?": "the verb for what happens",
    r"going forward": "delete",
    r"holistic(ally)?": "delete",
    r"cutting[- ]edge|state[- ]of[- ]the[- ]art|best[- ]in[- ]class": "delete",
    r"game[- ]chang(er|ing)": "delete",
    r"great (catch|question|point)": "delete; state what changed",
    r"happy to help|hope this helps|let me know if": "delete",
    r"i apologi[sz]e|sorry for the confusion": "delete; state the correction",
}

# Details that never belong in public text.
INTERNAL = {
    r"/(home|Users)/[\w.-]+": "a home directory path",
    r"/opt/[\w.-]+": "a host path",
    r"\b(10\.\d+|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+\b": "a private IP address",
    r"\b[\w-]+\.(internal|corp|lan)\b": "an internal host name",
}

_CHECKS = [
    (re.compile(rf"\b({pattern})\b", re.IGNORECASE), f"-> {fix}") for pattern, fix in FILLER.items()
] + [(re.compile(pattern), f"-> remove {what}") for pattern, what in INTERNAL.items()]
_INLINE_CODE = re.compile(r"`[^`]*`")


def check(text: str) -> list[tuple[int, str, str]]:
    hits = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        prose = _INLINE_CODE.sub("", line)
        for pattern, advice in _CHECKS:
            for match in pattern.finditer(prose):
                hits.append((number, match.group(0), advice))
    return hits


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    found = False
    for path in paths:
        text = sys.stdin.read() if path == "-" else Path(path).read_text()
        for number, phrase, advice in check(text):
            print(f"{path}:{number}: {phrase} {advice}")
            found = True
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
