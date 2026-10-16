"""The server's version is single-sourced, and every surface reports it."""

from __future__ import annotations

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
