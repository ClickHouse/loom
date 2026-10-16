"""The files git tracks, for tests that scan the repository.

`npx skills add` and local tooling install ignored files beside tracked ones (under
`.claude/skills/`, for one); a scan that walks the disk would judge them too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tracked(*paths: str) -> list[str]:
    if not (ROOT / ".git").exists():  # an sdist or tarball: nothing was installed beside it
        roots = [ROOT / p for p in paths] or [ROOT]
        files = [f for r in roots for f in ([r] if r.is_file() else r.rglob("*")) if f.is_file()]
        return sorted(str(f.relative_to(ROOT)) for f in files)
    out = subprocess.run(
        ["git", "ls-files", *paths], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()
