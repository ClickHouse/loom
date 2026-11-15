"""The Cursor plugin at the repository root: `.cursor-plugin/` holds the marketplace, the
manifest and its MCP entry, and the plugin's skills are the root `skills/`."""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

from server.cli._client import DEFAULT_BASE_URL
from server.cli.install import _install_mcp_url
from server.identity.app.token_clients import CODING_TOOLSET
from tests.unit.test_loom_code_skill import ROOT_SKILLS

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / ".cursor-plugin"
MANIFEST = json.loads((PLUGIN_DIR / "plugin.json").read_text())
MARKETPLACE = json.loads((PLUGIN_DIR / "marketplace.json").read_text())


def test_the_marketplace_lists_the_root_plugin_by_its_name() -> None:
    (entry,) = MARKETPLACE["plugins"]
    assert entry["source"] == "./"
    assert entry["name"] == MANIFEST["name"]


def test_manifest_paths_are_relative_and_exist() -> None:
    # Cursor's review refuses an absolute path or one that leaves the plugin.
    for field in ("logo", "skills", "mcpServers"):
        path = MANIFEST[field]
        assert not path.startswith("/") and not posixpath.normpath(path).startswith(".."), path
        assert (ROOT / path).exists(), path


def test_every_variable_the_mcp_entry_uses_is_declared_and_required() -> None:
    entry = (ROOT / MANIFEST["mcpServers"]).read_text()
    used = set(re.findall(r"\$\{(\w+)\}", entry))
    variables = MANIFEST["variables"]
    assert used and used == set(variables["properties"]) == set(variables["required"])


def test_the_default_url_is_the_one_loom_install_writes_for_cursor() -> None:
    # The entry has no Authorization header: a single-user server on the machine takes the
    # call without one, and a server with OAuth on answers 401 with the metadata pointer
    # Cursor follows to its browser sign-in.
    entry = json.loads((ROOT / MANIFEST["mcpServers"]).read_text())["mcpServers"]["loom"]
    assert entry == {"url": "${LOOM_MCP_URL}"}
    default = MANIFEST["variables"]["properties"]["LOOM_MCP_URL"]["default"]
    assert default == _install_mcp_url(DEFAULT_BASE_URL, CODING_TOOLSET)


def test_the_plugin_ships_the_root_skills() -> None:
    names = {p.parent.name for p in (ROOT / MANIFEST["skills"]).glob("*/SKILL.md")}
    assert names == set(ROOT_SKILLS)
