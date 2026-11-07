"""git in a repository a test makes, signing nothing whatever the host's config says."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgSign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )  # fmt: skip
