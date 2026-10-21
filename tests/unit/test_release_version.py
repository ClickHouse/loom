"""The server's version is set in server/__init__.py; every surface and the registry listing carry it."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import server

ROOT = Path(__file__).resolve().parents[2]


def test_server_version_matches_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == server.__version__


def test_every_surface_reports_the_server_version() -> None:
    from server.cli.doctor import _version
    from server.main import app
    from server.transport.mcp_config import SERVER_INFO

    assert app.version == _version() == SERVER_INFO["version"] == server.__version__


def test_registry_listing_carries_the_server_version() -> None:
    """server.json is the MCP Registry listing; the release job publishes it
    at the tag's version, so the committed copy follows the server."""
    listing = json.loads((ROOT / "server.json").read_text())
    assert listing["version"] == server.__version__
    assert listing["name"] == "io.github.ClickHouse/loom"
    assert len(listing["description"]) <= 100, "the registry caps a description at 100 characters"
    (remote,) = listing["remotes"]
    assert remote["type"] == "streamable-http"
    assert remote["url"] == "https://{loom_host}/mcp"
