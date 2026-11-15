"""examples/course-starter is copied out of this repository into a teacher's own, so
it reaches Loom's files by URL: those URLs must name files and headings that exist,
and its dev container must stay the root one's, which installs the same stack."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.unit.markdown_links import anchors
from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]
STARTER = ROOT / "examples" / "course-starter"
URL = re.compile(
    r"https://(?:github\.com/ClickHouse/loom/blob|raw\.githubusercontent\.com/ClickHouse/loom)"
    r"/main/([\w./-]+)(?:#([\w-]+))?"
)
# What the starter's postCreateCommand runs instead of the root's.
OWN_SETUP = {"name", "postCreateCommand"}


def devcontainer(path: Path) -> dict:
    # Both files hold only whole-line // comments.
    lines = [line for line in path.read_text().splitlines() if not line.strip().startswith("//")]
    return json.loads("\n".join(lines))


def test_devcontainer_matches_the_root_one() -> None:
    root = devcontainer(ROOT / ".devcontainer" / "devcontainer.json")
    starter = devcontainer(STARTER / ".devcontainer" / "devcontainer.json")
    assert {k: v for k, v in starter.items() if k not in OWN_SETUP} == {
        k: v for k, v in root.items() if k not in OWN_SETUP
    }


@pytest.mark.parametrize("path", tracked("examples/course-starter"))
def test_loom_urls_resolve(path: str) -> None:
    broken = []
    for match in URL.finditer((ROOT / path).read_text()):
        target, anchor = ROOT / match[1], match[2]
        if not target.is_file():
            broken.append(match[0])
        elif anchor and anchor.lower() not in anchors(target):
            broken.append(match[0])
    assert not broken, broken
