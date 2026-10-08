"""The dotenv file configuration loads from.

Lives in core, not settings, so the CLI can load the file into os.environ
before anything imports `server.settings` (whose load reads provider keys
from the environment).
"""

from __future__ import annotations

import os


def env_file_path() -> str | None:
    """LOOM_ENV_FILE, else `.env` in the working directory; empty reads none."""
    return os.environ.get("LOOM_ENV_FILE", ".env") or None
