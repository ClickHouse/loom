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


def settings_file_path(env_file: str | os.PathLike[str] | None) -> str | None:
    """The YAML settings file LOOM_SETTINGS_FILE names, in the environment or else in
    `env_file`, the dotenv file being loaded (the CLI and compose copy that file into
    the environment, so every process must read it the same way); unset or empty reads
    none. No default path: a `loom.yaml` in whatever directory the `loom` CLI runs in
    is not the server's."""
    if "LOOM_SETTINGS_FILE" in os.environ:
        return os.environ["LOOM_SETTINGS_FILE"] or None
    if env_file is None or not os.path.isfile(env_file):
        return None
    from dotenv import dotenv_values

    return dotenv_values(env_file).get("LOOM_SETTINGS_FILE") or None
