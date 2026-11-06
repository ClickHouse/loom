#!/usr/bin/env python3
"""Print `true` when a release should be the repository's Latest, else `false`.

    GH_TOKEN=... GITHUB_REPOSITORY=ClickHouse/loom python3 scripts/release/make_latest.py 1.2.3

GitHub's `releases/latest` serves the installer and `:latest` the image, so only
the highest final version takes them: a patch to an older line, or a
prerelease, leaves Latest where it is. It reads the repository's tags with
`gh`: a tag exists from its push on, before its release run publishes anything,
so two releases running at once agree. Prerelease tags never count. A failed
listing fails the script, never reads as "no tags".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

FINAL = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def is_latest(version: str, published: list[str]) -> bool:
    mine = FINAL.fullmatch(version)
    if not mine:
        return False
    finals = [m for tag in published if (m := FINAL.fullmatch(tag.strip()))]
    newest = max((tuple(map(int, m.groups())) for m in finals), default=(0, 0, 0))
    return tuple(map(int, mine.groups())) >= newest


def tags(repository: str) -> list[str]:
    out = subprocess.run(
        ["gh", "api", f"repos/{repository}/git/matching-refs/tags/v", "--paginate",
         "--jq", '.[].ref | ltrimstr("refs/tags/")'],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return out.split()


if __name__ == "__main__":
    published = tags(os.environ["GITHUB_REPOSITORY"])
    print("true" if is_latest(sys.argv[1], published) else "false")
