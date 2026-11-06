#!/usr/bin/env python3
"""Print `true` when a release should be the repository's Latest, else `false`.

    gh api repos/OWNER/REPO/releases --paginate --jq '.[] | select(.draft | not) | .tag_name' \
        | python3 scripts/release/make_latest.py 1.2.3

GitHub's `releases/latest` serves the installer, so only the highest final
version is Latest: a patch to an older line released after a newer minor, or a
prerelease, leaves Latest where it is. The published tags come on stdin; the
other prereleases among them never count.
"""

from __future__ import annotations

import re
import sys

FINAL = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def is_latest(version: str, published: list[str]) -> bool:
    mine = FINAL.fullmatch(version)
    if not mine:
        return False
    finals = [m for tag in published if (m := FINAL.fullmatch(tag.strip()))]
    newest = max((tuple(map(int, m.groups())) for m in finals), default=(0, 0, 0))
    return tuple(map(int, mine.groups())) >= newest


if __name__ == "__main__":
    print("true" if is_latest(sys.argv[1], sys.stdin.read().split()) else "false")
