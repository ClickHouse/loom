"""The plane is `tracker` on every surface: no op, route, MCP tool, toolset,
env key or SDK module answers to a `qa` name."""

from __future__ import annotations

import importlib

import pytest

from server.operations.registry import ALL_OPERATIONS
from server.operations.toolsets import is_toolset
from server.settings import Settings
from server.transport import mcp_call


def test_no_op_route_or_tool_is_named_qa() -> None:
    from server.transport.http import build_router

    assert not [op.name for op in ALL_OPERATIONS if op.name.startswith("qa.")]
    assert not [r.path for r in build_router().routes if r.path.startswith("/v1/qa.")]
    assert not [n for n in mcp_call.TOOLS_BY_NAME if n.startswith("loom_qa_")]
    assert "loom_tracker_duplicate_check" in mcp_call.TOOLS_BY_NAME


def test_every_source_op_is_served_under_its_tracker_name() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.http import build_router

    paths = {r.path for r in build_router().routes}
    for name in (
        "tracker.source.list",
        "tracker.source.add",
        "tracker.source.check",
        "tracker.source.rotate",
        "tracker.source.remove",
        "tracker.source.inspect",
        "tracker.source.set_mapping",
    ):
        assert name in OPERATIONS_BY_NAME and f"/v1/{name}" in paths, name


def test_qa_is_not_a_toolset() -> None:
    assert is_toolset("tracker") and not is_toolset("qa")


def test_settings_read_only_the_tracker_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_TRACKER_ORG", raising=False)
    monkeypatch.setenv("LOOM_QA_ORG", "acme")
    assert Settings(_env_file=None).tracker_org != "acme"
    monkeypatch.setenv("LOOM_TRACKER_ORG", "acme")
    assert Settings(_env_file=None).tracker_org == "acme"


def test_the_sdk_has_no_qa_module_or_attribute() -> None:
    from loom.agent import Agent

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("loom.qa")
    assert not hasattr(Agent, "qa")
