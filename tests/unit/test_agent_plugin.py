"""The Agent Plugins package at the repository root: `plugin.json` is the manifest GitHub
Copilot CLI installs, `mcp.json` its MCP entry, and its skills are the root `skills/`
(`tests/unit/test_cursor_plugin.py` checks that directory)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "plugin.json").read_text())
MCP = json.loads((ROOT / "mcp.json").read_text())
CURSOR = json.loads((ROOT / ".cursor-plugin" / "plugin.json").read_text())

# Agent Plugins 1.0.0 refuses any other top-level field; it has no skills field, and a
# client loads the skills/ directory next to plugin.json.
MANIFEST_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}


def test_the_manifest_holds_only_agent_plugins_fields() -> None:
    assert MANIFEST["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert set(MANIFEST) <= MANIFEST_FIELDS
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", MANIFEST["name"])


def test_the_docs_uninstall_both_install_names() -> None:
    # A repository install is named by plugin.json; Copilot CLI names a marketplace install
    # after the .claude-plugin marketplace entry whose source is the repository root.
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    (root_entry,) = [e for e in marketplace["plugins"] if e["source"] == "./"]
    docs = " ".join((ROOT / "docs/features/integrations/mcp-clients.md").read_text().split())
    for name in (MANIFEST["name"], root_entry["name"]):
        assert f"`copilot plugin uninstall {name}`" in docs


def test_the_entry_is_the_local_server_with_no_headers() -> None:
    # Agent Plugins forbids credentials in headers; a single-user server needs none.
    assert MCP["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    entry = MCP["mcpServers"]["loom"]
    default = CURSOR["variables"]["properties"]["LOOM_MCP_URL"]["default"]
    assert entry == {"type": "streamable-http", "url": default}
