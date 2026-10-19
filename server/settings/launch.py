"""`python -m server.settings.launch NAME` prints the value of setting NAME as the
server will load it (environment, env file, then settings file), or "" when none of
them sets it: scripts/serve.sh reads the fields uvicorn needs before the server
starts, so a value in the settings file starts the same web tier as one in .env."""

from __future__ import annotations

import sys

from server.settings import settings


def configured(name: str) -> str:
    sources = ("env", "env_file", "settings_file")
    if not any(name in settings.loaded_from(s) for s in sources):
        return ""
    return str(getattr(settings, name))


if __name__ == "__main__":
    print(configured(sys.argv[1]))
