"""The pinned Flask indexer invocation for a source-only compiler tree."""

from __future__ import annotations

import re
from pathlib import Path


def index_command(root: Path, executable: Path, *, base_commit: str) -> list[str]:
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise ValueError("SCIP project version requires the full public base commit")
    return [
        str(executable),
        "index",
        ".",
        "--project-name",
        "flask",
        "--project-version",
        base_commit,
        "--environment",
        str(root / "environment.json"),
        "--output",
        str(root / "index.scip"),
    ]
