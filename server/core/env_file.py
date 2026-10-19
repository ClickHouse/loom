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


# The settings file read when LOOM_SETTINGS_FILE is unset, if it exists.
DEFAULT_SETTINGS_FILE = "loom.yaml"


def settings_file_path() -> tuple[str | None, bool]:
    """The YAML settings file and whether it was named explicitly:
    LOOM_SETTINGS_FILE, else `loom.yaml` in the working directory; empty reads none."""
    named = os.environ.get("LOOM_SETTINGS_FILE")
    if named is None:
        return DEFAULT_SETTINGS_FILE, False
    return named or None, True


if __name__ == "__main__":
    # `python -m server.core.env_file NAME` prints NAME's value in the env file
    # ("" when the file or the name is missing), for scripts/serve.sh.
    import sys

    from dotenv import dotenv_values

    path = env_file_path()
    values = dotenv_values(path) if path and os.path.isfile(path) else {}
    print(values.get(sys.argv[1]) or "")
