"""The files configuration loads from: the dotenv file and the YAML settings file.

Lives in core, not settings, so the CLI can load the file into os.environ
before anything imports `server.settings` (whose load reads provider keys
from the environment).
"""

from __future__ import annotations

import os


def env_file_path() -> str | None:
    """LOOM_ENV_FILE, else `.env` in the working directory; empty reads none."""
    return os.environ.get("LOOM_ENV_FILE", ".env") or None


def settings_file_path() -> str | None:
    """The YAML settings file LOOM_SETTINGS_FILE names; unset or empty reads none.
    No default path: a `loom.yaml` in whatever directory the `loom` CLI runs in is
    not the server's."""
    return os.environ.get("LOOM_SETTINGS_FILE") or None
