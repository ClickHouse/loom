#!/usr/bin/env python3
"""Render docs/getting-started/agent-setup.md as the release's agent-setup.txt.

    python3 scripts/release/agent_setup.py --version 1.2.3 --commit <sha> \
        --out release/agent/agent-setup.txt

The text is the page with the release's version and commit filled in for
`<version>` and `<commit>`, and each relative link written out as its page at
the release's tag, so an agent that reads only this file can follow every step.
"""

from __future__ import annotations

import argparse
import posixpath
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs/getting-started/agent-setup.md"
REPO_URL = "https://github.com/ClickHouse/loom/blob/v{version}/"
PINS = re.compile(r"<!-- pins -->\n.*?<!-- /pins -->\n", re.S)
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
SCRIPT = re.compile(r'```python title="loom-setup-check.py"\n(.*?)```', re.S)


def _absolute(match: re.Match[str], version: str) -> str:
    text, target = match.groups()
    if target.startswith("#"):
        return text  # a section of this same text
    if "://" not in target:
        path = posixpath.normpath(posixpath.join(PAGE.parent.relative_to(ROOT).as_posix(), target))
        target = REPO_URL.format(version=version) + path
    return f"{text} ({target})"


def render(page: str, version: str, commit: str) -> str:
    """The page with the release's version and commit filled in and every link spelled out."""
    pins = f"This text is for Loom `{version}`, tagged `v{version}` at commit `{commit}`.\n"
    text, found = PINS.subn(lambda _: pins, page)
    if not found:
        raise ValueError("the page has no <!-- pins --> block")
    text = text.replace("<version>", version).replace("<commit>", commit)
    return LINK.sub(lambda m: _absolute(m, version), text)


def check_script(text: str) -> str:
    """The page's (or the rendered text's) loom-setup-check.py."""
    found = SCRIPT.search(text)
    if not found:
        raise ValueError("no loom-setup-check.py block")
    return found[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(PAGE.read_text(), args.version, args.commit))


if __name__ == "__main__":
    main()
