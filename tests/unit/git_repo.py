"""git in a repository a test makes, signing nothing whatever the host's config says."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> None:
    """Raises with git's own message when it fails."""
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgSign=false", *args],
        cwd=repo, capture_output=True, text=True,
    )  # fmt: skip
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
