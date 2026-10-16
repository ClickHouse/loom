"""The files git tracks, for tests that scan the repository.

`npx skills add` and local tooling install ignored files beside tracked ones (under
`.claude/skills/`, for one); a scan that walks the disk would judge them too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def tracked(*paths: str) -> list[str]:
    if not (ROOT / ".git").exists():  # an sdist or tarball: nothing is tracked
        pytest.skip("not a git checkout", allow_module_level=True)
    out = subprocess.run(
        ["git", "ls-files", *paths], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()
