"""gemini-extension.json: the Gemini CLI extension that connects to a Loom server over MCP."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "gemini-extension.json").read_text())


def test_every_variable_the_server_entry_uses_is_a_setting() -> None:
    entry = MANIFEST["mcpServers"]["loom"]
    used = set(re.findall(r"\$\{(\w+)\}", json.dumps(entry)))
    declared = {s["envVar"] for s in MANIFEST["settings"]}
    assert used and used == declared


def test_the_token_is_a_sensitive_setting_sent_as_a_bearer() -> None:
    # An empty token sends "Bearer ": a single-user (open) server serves the MCP call with no
    # auth and logs a warning (server/transport/mcp.py); a team server answers 401.
    assert MANIFEST["mcpServers"]["loom"]["headers"] == {"Authorization": "Bearer ${LOOM_API_KEY}"}
    token = next(s for s in MANIFEST["settings"] if s["envVar"] == "LOOM_API_KEY")
    assert token["sensitive"] is True


def test_the_extension_name_and_install_line_match_the_docs() -> None:
    page = (ROOT / "docs/features/integrations/mcp-clients.md").read_text()
    assert f"gemini extensions config {MANIFEST['name']}" in page
    assert "gemini extensions install https://github.com/ClickHouse/loom" in page
