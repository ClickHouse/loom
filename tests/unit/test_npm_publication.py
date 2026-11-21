"""The SDK and JavaScript adapters use one npm publication path."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_release_workflow_is_the_only_npm_publisher() -> None:
    publishers = []
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job, config in workflow.get("jobs", {}).items():
            for step in config.get("steps", []):
                if re.search(r"\b(?:npm|pnpm|yarn) publish\b", step.get("run", "")):
                    publishers.append((path.name, job))
    assert publishers == [("release.yml", "npm")]
