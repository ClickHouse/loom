"""Independent extension tags must match both package manifests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("tag", "version", "manifest_version", "name", "accepted"),
    [
        ("lfx-loom-v0.1.0", "0.1.0", "0.1.0", "lfx-loom", True),
        ("lfx-loom-v2.4.6", "2.4.6", "2.4.6", "lfx-loom", True),
        ("lfx-loom-v0.1.1", "0.1.0", "0.1.0", "lfx-loom", False),
        ("lfx-loom-v0.1.0", "0.1.0", "0.2.0", "lfx-loom", False),
        ("lfx-loom-v0.1.0", "0.1.0", "0.1.0", "another-project", False),
        ("lfx-loom-v0.1.0-rc.1", "0.1.0", "0.1.0", "lfx-loom", False),
        ("lfx-loom-v0.1.0", "0.1.0rc1", "0.1.0rc1", "lfx-loom", False),
        ("lfx-loom-v01.1.0", "01.1.0", "01.1.0", "lfx-loom", False),
        ("lfx-loom-v0.1", "0.1", "0.1", "lfx-loom", False),
        ("v1.3.0", "0.1.0", "0.1.0", "lfx-loom", False),
        ("lfx-loom-v0.1.0; exit 0", "0.1.0", "0.1.0", "lfx-loom", False),
    ],
)
def test_tag_identifies_the_extension_in_both_manifests(
    tmp_path: Path, tag: str, version: str, manifest_version: str, name: str, accepted: bool
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/langflow-publish.yml").read_text())
    guard = next(
        step["run"]
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("id") == "publication-tag"
    )
    source = tmp_path / "integrations/langflow"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "{version}"\n')
    (source / "extension.json").write_text(
        json.dumps({"id": "lfx-loom", "version": manifest_version})
    )
    search_path = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", guard],
        cwd=tmp_path,
        env={**os.environ, "RELEASE_TAG": tag, "IS_TAG": "true", "PATH": search_path},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (result.returncode == 0) is accepted, result.stderr


@pytest.mark.parametrize("on_main", [True, False])
def test_publication_rejects_a_commit_not_shipped_on_main(tmp_path: Path, on_main: bool) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/langflow-publish.yml").read_text())
    guard = next(
        step["run"]
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("id") == "publication-source"
    )
    commit = [
        "git",
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.com",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "fixture",
    ]
    for command in (
        ["git", "init", "--quiet"],
        commit,
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, timeout=5)
    if not on_main:
        subprocess.run(commit, cwd=tmp_path, check=True, capture_output=True, timeout=5)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", guard],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (result.returncode == 0) is on_main, result.stderr
