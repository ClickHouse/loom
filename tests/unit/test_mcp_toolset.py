"""MCP tool-surface gating (settings.mcp_toolset).

`core` (default) shows a focused chat surface; `all` shows the full
registry surface (minus the always-hidden operator ops). Gating is
visibility-only — these tests assert what `tools/list` projects, not what
`tools/call` permits.

Hermetic: `_caller_default_context` (which can hit ClickHouse) is patched
to return an empty preamble, so no CH is required.
"""

from __future__ import annotations

import asyncio

import pytest

import server.transport.mcp as mcp
from server.operations.toolsets import _MCP_CORE_OPS, _MCP_HIDDEN_OPS
from server.settings import settings
from server.transport import mcp_primer
from server.transport.mcp_schemas import _shape_schema


@pytest.fixture(autouse=True)
def _no_ch_default_context(monkeypatch):
    async def _empty(auth, defaults=None):
        return ""

    monkeypatch.setattr(mcp_primer, "_caller_default_context", _empty)
    # Pin the code-tools flag: conftest auto-loads the repo .env, so a
    # deployment that flipped LOOM_MCP_CODE_TOOLS=true would grow the core
    # listing past this file's size assertions. These tests assert the
    # BASE surface; the flag's additive effect is covered separately.
    monkeypatch.setattr(settings, "mcp_code_tools", False)


def _tool_names(toolset: str) -> set[str]:
    # _tools_descriptor reads the settings singleton lazily; patch the
    # singleton attribute so both this test and the function see it.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "mcp_toolset", toolset)
    try:
        descriptors = asyncio.run(mcp._tools_descriptor())
    finally:
        monkey.undo()
    return {d["name"] for d in descriptors}


def test_core_surface_is_small_and_focused():
    names = _tool_names("core")
    # Synthetic front doors are always present.
    assert "loom_search" in names
    assert "loom_activate" in names
    # Core save/recall/manage tools present.
    for expected in (
        "loom_memory_set",
        "loom_memory_get",
        "loom_memory_pin",
        "loom_memory_apply",
        "loom_context_pack",
    ):
        assert expected in names, expected
    # The listing carries the chat loop, not the
    # whole product surface — every slot is a per-turn tax. These moved to
    # callable-by-name: render (context.pack already loads the profile),
    # get_by_id (conflict-resolution flow), namespace membership ops
    # (onboarding-time actions), and the inventory, profile, namespace and
    # erasure ops a chat loop reaches for rarely.
    for demoted in (
        "loom_profile_render",
        "loom_profile_set",
        "loom_memory_list",
        "loom_memory_set_from_messages",
        "loom_working_memory_replace",
        "loom_knowledge_add",
        "loom_conversation_append",
        "loom_namespace_list_mine",
        "loom_erasure_request",
        "loom_memory_get_by_id",
        "loom_namespace_join",
        "loom_namespace_leave",
        "loom_namespace_browse",
    ):
        assert demoted not in names, demoted
    # Thirteen at most: every listed tool is read on every turn. Documents,
    # the scratchpad and conversation capture run through loom_op.
    assert len(names) <= 13, f"core surface too large: {len(names)}"
    # The Refuse verb must be reachable in-band ("never remember X").
    assert "loom_blacklist_add" in names


def test_core_hides_advanced_ops():
    names = _tool_names("core")
    # Advanced/specialized ops are not in the core chat surface.
    for hidden in (
        "loom_decision_record",
        "loom_facts_traverse",
        "loom_procedure_add",
        "loom_grants_create",
    ):
        assert hidden not in names, hidden


def test_all_surface_restores_advanced_ops():
    names = _tool_names("all")
    assert "loom_conversation_append" in names
    assert "loom_facts_traverse" in names
    # And it's meaningfully larger than core.
    assert len(names) > len(_tool_names("core"))
    # An op with no production caller in the 30-day census is
    # experimental — callable by name, not listed under `all`.
    assert "loom_decision_record" not in names


def test_hidden_ops_never_appear_in_either_toolset():
    for toolset in ("core", "coding", "atomic", "all"):
        names = _tool_names(toolset)
        for op_name in ("memory.search", "namespace.list", "audit.list"):
            # _MCP_HIDDEN_OPS uses op.name; the projected tool name is the
            # loom_ form. Neither should surface.
            assert op_name in _MCP_HIDDEN_OPS
            assert f"loom_{op_name.replace('.', '_')}" not in names


def test_core_ops_are_a_subset_of_visible_all_ops():
    """Every core op must also be visible in `all` (core ⊆ all)."""
    all_names = _tool_names("all")
    for op_name in _MCP_CORE_OPS:
        assert op_name not in _MCP_HIDDEN_OPS, f"{op_name} is both core and hidden"
        assert f"loom_{op_name.replace('.', '_')}" in all_names, op_name


def test_shape_schema_applies_the_three_node_fixes():
    """The merged schema walker still strips episodic, drops date-time
    format, and widens integers — recursively, leaving other nodes intact."""
    schema = {
        "type": "object",
        "properties": {
            "memory_type": {"enum": ["semantic", "episodic", "procedural"]},
            "created_at": {"type": "string", "format": "date-time"},
            "count": {"type": "integer"},
            "name": {"type": "string"},
            "nested": {"type": "object", "properties": {"n": {"type": "integer"}}},
        },
    }
    out = _shape_schema(schema)
    props = out["properties"]
    assert props["memory_type"]["enum"] == ["semantic", "procedural"]
    assert "format" not in props["created_at"] and props["created_at"]["type"] == "string"
    assert props["count"] == {"type": ["integer", "string"]}
    assert props["name"] == {"type": "string"}  # untouched
    assert props["nested"]["properties"]["n"]["type"] == ["integer", "string"]  # recursive
    # Pure: the input dict is not mutated in place.
    assert schema["properties"]["count"] == {"type": "integer"}
