"""Test-only ASGI composition: redirect upstream HTTP, not validation or storage.

Mounted by the disposable Docker gate, never shipped as Loom's entrypoint.
Each spawned web worker imports this module independently.
"""

from server.settings import settings

settings.openai_base_url = "http://provider-smoke:8080/v1"

from server.main import app  # noqa: E402, F401 — the real ASGI app, after transport substitution
