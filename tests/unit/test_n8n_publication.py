"""Publication tags select the independent package version without releasing the server."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("tag", "version", "accepted"),
    [
        ("n8n-v0.1.0", "0.1.0", True),
        ("n8n-v2.4.6", "2.4.6", True),
        ("n8n-v0.1.1", "0.1.0", False),
        ("n8n-v0.1.0-rc.1", "0.1.0", False),
        ("n8n-v0.1.0", "0.1.0-rc.1", False),
        ("n8n-v01.1.0", "01.1.0", False),
        ("v1.3.0", "0.1.0", True),
        ("v1.3.0", "0.1.0-rc.1", False),
        ("v1.3.0-rc.1", "0.1.0", False),
        ("n8n-v0.1.0; exit 0", "0.1.0", False),
    ],
)
def test_publication_tag_selects_a_stable_package(
    tmp_path: Path, tag: str, version: str, accepted: bool
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/n8n-publish.yml").read_text())
    guard = next(
        step["run"]
        for step in workflow["jobs"]["publish"]["steps"]
        if step.get("id") == "publication-tag"
    )
    (tmp_path / "package.json").write_text(json.dumps({"version": version}))
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", guard],
        cwd=tmp_path,
        env={**os.environ, "RELEASE_TAG": tag},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (result.returncode == 0) is accepted, result.stderr
