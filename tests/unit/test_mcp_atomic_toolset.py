"""The atomic toolset: four verbs, the scratchpad, the run
ledger — and descriptions written as a policy."""

from __future__ import annotations

import asyncio

import pytest

import server.transport.mcp as mcp
from server.operations.toolsets import _MCP_ATOMIC_OPS
from server.settings import settings
from server.transport import mcp_primer


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(*_a, **_k) -> str:
        return ""

    monkeypatch.setattr(mcp_primer, "_caller_default_context", _empty)
    monkeypatch.setattr(settings, "mcp_code_tools", True)  # atomic ignores code tools


def _descriptors(toolset: str):
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "mcp_toolset", toolset)
    try:
        return asyncio.run(mcp._tools_descriptor())
    finally:
        monkey.undo()


def test_atomic_surface_is_the_four_verbs_plus_scratchpad_and_ledger() -> None:
    ds = _descriptors("atomic")
    names = {d["name"] for d in ds}
    assert names == {
        "loom_search",
        "loom_activate",
        "loom_op",
        "loom_memory_set",
        "loom_memory_get",
        "loom_memory_delete",
        "loom_memory_ask",
        "loom_working_memory_set",
        "loom_working_memory_append",
        "loom_run_begin",
        "loom_run_complete",
    } | ({"loom_code_search"} & names)  # never: code tools stay out
    assert not any(n.startswith("loom_code_") for n in names)
    # the count derives from the roster: the atomic ops plus the three synthetic tools
    assert len(names) == len(_MCP_ATOMIC_OPS) + 3
    # atomic ⊆ all: its scratchpad and ledger tools are not on core
    every = {d["name"] for d in _descriptors("all")}
    assert names < every | {"loom_memory_ask", "loom_run_begin", "loom_run_complete"}


def test_atomic_descriptions_are_the_policy_text() -> None:
    by = {d["name"]: d["description"] for d in _descriptors("atomic")}
    assert "UPDATE one by passing the memory_key" in by["loom_memory_set"]
    assert "if_version" in by["loom_memory_get"]
    assert "SCRATCHPAD" in by["loom_working_memory_set"]
    assert "memories_used" in by["loom_run_complete"]
    # the core surface keeps its own text
    core_by = {d["name"]: d["description"] for d in _descriptors("core")}
    assert core_by["loom_memory_set"] != by["loom_memory_set"]


def test_other_toolsets_are_unchanged() -> None:
    assert len(_descriptors("core")) >= 20
    assert len(_descriptors("all")) > len(_descriptors("core")) > len(_descriptors("atomic"))
