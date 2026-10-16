"""The files git tracks, for tests that scan the repository.

`npx skills add` and local tooling install ignored files beside tracked ones (under
`.claude/skills/`, for one); a scan that walks the disk would judge them too.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tracked(*paths: str, root: Path = ROOT) -> list[str]:
    if not (root / ".git").exists():  # a source archive: what it holds is what git tracked
        return _on_disk(root, paths)
    out = subprocess.run(
        ["git", "ls-files", *paths], cwd=root, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def _on_disk(root: Path, paths: tuple[str, ...]) -> list[str]:
    # Files and symlinks, as git lists them; a linked directory is one entry, not walked.
    found = []
    for top in [root / p for p in paths] or [root]:
        if top.is_file() or top.is_symlink():
            found.append(top)
        for here, dirs, files in os.walk(top):
            found += [Path(here, n) for n in files]
            found += [Path(here, n) for n in dirs if Path(here, n).is_symlink()]
    return sorted(str(f.relative_to(root)) for f in found)
