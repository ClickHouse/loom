"""scripts/release/preflight.sh and make_latest.py: the release's owner-granted
prerequisites, and which release becomes Latest."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
from make_latest import is_latest  # noqa: E402

# A stub curl answering as ghcr.io does: an anonymous pull token only for a
# public package, a push token for anyone signed in, and an upload session
# (202 with a Location) only for a writer. Each call is logged.
FAKE_CURL = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
url=${@: -1}
case "$*" in
  *"-X DELETE"*) exit 0 ;;
  *"-X POST"*)
    pkg=${url#*/v2/}; pkg=${pkg%/blobs/uploads/}
    if [[ " $WRITABLE " == *" $pkg "* ]]; then
      printf 'HTTP/2 202\r\nlocation: /v2/%s/blobs/uploads/abc\r\n\r\n' "$pkg"
    else
      printf 'HTTP/2 403\r\n\r\n'
    fi ;;
  *" -u "*) echo '{"token":"t"}' ;;
  *)
    pkg=${url#*scope=repository:}; pkg=${pkg%%:*}
    [[ " $PUBLIC " == *" $pkg "* ]] || exit 22
    echo '{"token":"anon"}' ;;
esac
"""

IMAGES = "ghcr.io/clickhouse/loom ghcr.io/clickhouse/loom-extractor"


def _preflight(
    tmp_path: Path, mode: str, *, private: str, writable: str, public: str
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "GITHUB_REPOSITORY": "ClickHouse/loom",
        "GITHUB_ACTOR": "release-bot",
        "GH_TOKEN": "token",
        "REPOSITORY_PRIVATE": private,
        "IMAGES": IMAGES,
        "WRITABLE": writable,
        "PUBLIC": public,
        "GHCR": "https://registry.test",
    }
    return subprocess.run(
        ["bash", str(ROOT / "scripts/release/preflight.sh"), mode],
        env=env,
        capture_output=True,
        text=True,
    )


def test_a_public_repository_with_both_packages_ready_passes(tmp_path: Path) -> None:
    both = "clickhouse/loom clickhouse/loom-extractor"
    result = _preflight(tmp_path, "strict", private="false", writable=both, public=both)
    assert result.returncode == 0, result.stdout
    assert "0 release prerequisite(s) missing" in result.stdout
    # The probe's upload session is cancelled, so nothing is left in the registry.
    calls = (tmp_path / "calls").read_text()
    assert calls.count("-X DELETE") == 2
    assert "https://registry.test/v2/clickhouse/loom/blobs/uploads/abc" in calls


def test_each_missing_prerequisite_names_its_fix_and_fails_a_release(tmp_path: Path) -> None:
    """Today's state: a private repository, an org package this repository
    cannot write, and two private packages."""
    result = _preflight(
        tmp_path, "strict", private="true", writable="clickhouse/loom-extractor", public=""
    )
    assert result.returncode == 1
    errors = [line for line in result.stdout.splitlines() if line.startswith("::error::")]
    assert len(errors) == 5, result.stdout
    assert "ClickHouse/loom is private" in errors[0]
    assert "ghcr.io/clickhouse/loom: this workflow cannot push (HTTP 403)" in errors[1]
    assert "Manage Actions access, add ClickHouse/loom with the Write role" in errors[1]
    assert "packages/container/package/loom," in errors[1]
    assert "ghcr.io/clickhouse/loom: anyone cannot pull" in errors[2]
    assert "set the visibility to Public" in errors[2]
    assert "loom-extractor: anyone cannot pull" in errors[3]
    assert "4 release prerequisite(s) missing" in errors[4]
    assert "can push" in result.stdout  # the extractor


def test_a_dry_run_reports_the_same_prerequisites_as_warnings(tmp_path: Path) -> None:
    result = _preflight(tmp_path, "report", private="true", writable="", public="")
    assert result.returncode == 0
    assert "::error::" not in result.stdout
    assert result.stdout.count("::warning::") == 5
    assert "5 release prerequisite(s) missing" in result.stdout


def test_a_registry_that_gives_no_token_counts_as_no_push(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    result = _preflight(tmp_path, "strict", private="false", writable="", public="")
    assert "cannot push (HTTP 403)" in result.stdout


def test_the_workflow_runs_the_preflight_before_any_build() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    assert jobs["preflight"]["needs"] == "version"
    assert "preflight" in jobs["clickhouse"]["needs"]
    (step,) = [s for s in jobs["preflight"]["steps"] if "preflight.sh" in s.get("run", "")]
    assert step["env"]["MODE"] == "${{ inputs.dry_run && 'report' || 'strict' }}"
    assert step["env"]["REPOSITORY_PRIVATE"] == "${{ github.event.repository.private }}"
    matrix = jobs["docker"]["strategy"]["matrix"]["include"]
    assert step["env"]["IMAGES"].split() == [i["image"] for i in matrix if "image" in i]


@pytest.mark.parametrize(
    ("version", "published", "latest"),
    [
        ("1.2.0", [], True),
        ("1.2.0", ["v1.0.1", "v1.1.0"], True),
        ("1.2.0", ["v1.2.0"], True),  # a re-run of the latest
        ("1.1.3", ["v1.2.0", "v1.1.2"], False),  # a patch to the older line
        ("1.3.0-rc.1", ["v1.2.0"], False),
        ("1.2.1", ["v1.2.0", "v1.3.0-rc.1"], True),  # prereleases never count
        ("1.10.0", ["v1.9.4"], True),  # numeric, not text, order
    ],
)
def test_only_the_highest_final_version_becomes_latest(
    version: str, published: list[str], latest: bool
) -> None:
    assert is_latest(version, published) is latest


def test_the_image_and_the_release_take_latest_from_one_answer() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    assert jobs["version"]["outputs"]["latest"] == "${{ steps.latest.outputs.latest }}"
    (meta,) = [s for s in jobs["docker"]["steps"] if s.get("id") == "meta"]
    assert (
        "type=raw,value=latest,enable=${{ needs.version.outputs.latest == 'true' }}"
        in (meta["with"]["tags"])
    )
    (publish,) = [
        s for s in jobs["github-release"]["steps"] if "action-gh-release" in s.get("uses", "")
    ]
    assert publish["with"]["make_latest"] == "${{ needs.version.outputs.latest }}"


def test_a_dry_run_publishes_nothing() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    for name in ("docker-tag", "github-release", "pypi", "npm", "mcp-registry"):
        assert "!inputs.dry_run" in jobs[name]["if"], name
    (login,) = [s for s in jobs["docker"]["steps"] if s.get("name") == "Log in to ghcr.io"]
    assert login["if"] == "${{ !inputs.dry_run }}"
    (build,) = [s for s in jobs["docker"]["steps"] if s.get("id") == "build"]
    assert build["with"]["outputs"].startswith("${{ inputs.dry_run && format('type=docker,")
