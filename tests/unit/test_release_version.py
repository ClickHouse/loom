"""Every surface reports the server's version, and the registry listing keeps its shape.
That every file names one version is test_release_prepare.py's."""

from __future__ import annotations

import json
from pathlib import Path

import server

ROOT = Path(__file__).resolve().parents[2]


def test_every_surface_reports_the_server_version() -> None:
    from server.cli.doctor import _version
    from server.main import app
    from server.transport.mcp_config import SERVER_INFO

    assert app.version == _version() == SERVER_INFO["version"] == server.__version__


def test_registry_listing_keeps_its_shape() -> None:
    """server.json is the MCP Registry listing the release job publishes."""
    listing = json.loads((ROOT / "server.json").read_text())
    assert listing["name"] == "io.github.ClickHouse/loom"
    assert len(listing["description"]) <= 100, "the registry caps a description at 100 characters"
    (remote,) = listing["remotes"]
    assert remote["type"] == "streamable-http"
    assert remote["url"] == "https://{loom_host}/mcp"
