"""scripts/release/published_bundle.sh tells "never published" (exit 3) from a
failed lookup (exit 1), so a release re-run never moves an installed version's
ClickHouse; the release keeps a published bundle instead of replacing it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release" / "published_bundle.sh"

# `gh api` answers as STUB_RELEASE says; `gh release download` writes the bundle.
STUB_GH = """#!/usr/bin/env bash
if [ "$1" = api ]; then
  case "$STUB_RELEASE" in
    missing) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
    denied) echo "gh: Bad credentials (HTTP 401)" >&2; exit 1 ;;
    unbundled) echo install.sh ;;
    bundled) printf 'install.sh\\nloom-stack.tar.gz\\n' ;;
  esac
  exit 0
fi
while [ $# -gt 0 ]; do [ "$1" = --dir ] && dir=$2; shift; done
echo bundle > "$dir/loom-stack.tar.gz"
"""


def _run(tmp_path: Path, release: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(STUB_GH)
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_RELEASE": release,
        "GITHUB_REPOSITORY": "o/r",
    }
    out = tmp_path / "published"
    out.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(SCRIPT), "1.2.3", str(out)], env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize(("release", "status"), [("missing", 3), ("unbundled", 3), ("denied", 1)])
def test_only_an_unpublished_bundle_is_exit_3(tmp_path: Path, release: str, status: int) -> None:
    assert _run(tmp_path, release).returncode == status


def test_a_published_bundle_is_downloaded(tmp_path: Path) -> None:
    assert _run(tmp_path, "bundled").returncode == 0
    assert (tmp_path / "published" / "loom-stack.tar.gz").read_text() == "bundle\n"


def test_the_release_never_replaces_a_published_bundle() -> None:
    """Replacing an asset deletes it first; a failed upload would lose the pin."""
    steps = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())["jobs"][
        "github-release"
    ]["steps"]
    keep = next(i for i, s in enumerate(steps) if "published_bundle.sh" in s.get("run", ""))
    upload = next(i for i, s in enumerate(steps) if "action-gh-release" in s.get("uses", ""))
    assert keep < upload
    assert "rm release/stack/loom-stack.tar.gz" in steps[keep]["run"]
