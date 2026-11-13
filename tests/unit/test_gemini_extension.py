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


def test_the_header_is_one_setting_so_an_empty_token_sends_no_bearer() -> None:
    # Loom refuses "Bearer " with nothing after it, even with auth off; a blank header is none.
    assert MANIFEST["mcpServers"]["loom"]["headers"] == {"Authorization": "${LOOM_AUTHORIZATION}"}
    token = next(s for s in MANIFEST["settings"] if s["envVar"] == "LOOM_AUTHORIZATION")
    assert token["sensitive"] is True


def test_the_extension_name_and_install_line_match_the_docs() -> None:
    page = (ROOT / "docs/features/integrations/mcp-clients.md").read_text()
    assert MANIFEST["name"] == "clickhouse-loom"
    assert f"gemini extensions config {MANIFEST['name']}" in page
    assert "gemini extensions install https://github.com/ClickHouse/loom" in page
