"""Each SDK carries a changelog whose top entry is the
package's version, and the versions are single-sourced."""

from __future__ import annotations

import json
import re
from pathlib import Path

import loom

ROOT = Path(__file__).resolve().parents[2]
ENTRY_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (Unreleased|\d{4}-\d{2}-\d{2})$", re.M)


def _top_entry(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    m = ENTRY_RE.search(text)
    assert m, f"{path}: no `## [x.y.z] - YYYY-MM-DD|Unreleased` entry"
    return m.group(1), m.group(2)


def test_python_changelog_top_entry_is_the_package_version() -> None:
    version, _ = _top_entry(ROOT / "sdk" / "python" / "CHANGELOG.md")
    assert version == loom.__version__
    pyproject = (ROOT / "sdk" / "python" / "pyproject.toml").read_text()
    assert f'version = "{version}"' in pyproject
    assert "sdk/python/CHANGELOG.md" in pyproject, (
        "pyproject's Changelog URL must point at the SDK's changelog"
    )


def test_typescript_changelog_top_entry_is_the_package_version() -> None:
    ts = ROOT / "sdk" / "typescript"
    version, _ = _top_entry(ts / "CHANGELOG.md")
    pkg = json.loads((ts / "package.json").read_text())
    assert pkg["version"] == version
    src = (ts / "src" / "version.ts").read_text()
    assert f'export const SDK_VERSION = "{version}";' in src
    assert "CHANGELOG.md" in pkg["files"], "the changelog ships in the npm package"


def test_changelog_entries_are_ordered_newest_first() -> None:
    for rel in ("sdk/python/CHANGELOG.md", "sdk/typescript/CHANGELOG.md"):
        versions = [
            tuple(int(x) for x in v.split("."))
            for v, _ in ENTRY_RE.findall((ROOT / rel).read_text())
        ]
        assert versions == sorted(versions, reverse=True), f"{rel}: entries out of order {versions}"
        assert len(versions) == len(set(versions)), f"{rel}: duplicate entries"
