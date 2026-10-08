"""How the CLI writes the files that hold credentials. The checkout root
the commands read files beside is `server.core.paths.REPO_ROOT`."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private(path: Path, text: str) -> Path:
    """Replace `path` with `text`, owner-only (0600) from the first byte.

    A temp file beside `path` (mkstemp creates it 0600) is renamed over it, so
    a reader never sees a partial file and a crash leaves the old one. A
    missing parent is created 0700. For every file that holds a token or a
    secret: the token file, client MCP configs, `.env`."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
