"""`?toolset=readonly` lists the `all` surface projected to the
tools a VIEWER can actually call — a read-only registration never sees a write
tool that would only 403. Visibility-only (the token is the call-time boundary),
and URL-selected so the listing stays publicly cacheable."""

from __future__ import annotations

import asyncio

import pytest

import server.transport.mcp as mcp
from server.operations.base import Role
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings
from server.transport import mcp_primer

_TOOL_TO_OP = {op.mcp_tool_name: op for op in ALL_OPERATIONS}


@pytest.fixture(autouse=True)
def _no_ch_default_context(monkeypatch):
    async def _empty(auth):
        return ""

    monkeypatch.setattr(mcp_primer, "_caller_default_context", _empty)
    monkeypatch.setattr(settings, "mcp_code_tools", False)


def _tool_names(toolset: str) -> set[str]:
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "mcp_toolset", toolset)
    try:
        descriptors = asyncio.run(mcp._tools_descriptor(toolset=toolset))
    finally:
        monkey.undo()
    return {d["name"] for d in descriptors}


def test_readonly_is_a_subset_of_all() -> None:
    assert _tool_names("readonly") < _tool_names("all")


def test_readonly_keeps_reads_and_drops_writes() -> None:
    names = _tool_names("readonly")
    # loom_search is the synthetic search front-door (the raw memory.search op
    # is not listed under `all`); loom_memory_get / loom_facts_traverse are reads.
    for read_tool in ("loom_memory_get", "loom_search", "loom_facts_traverse"):
        assert read_tool in names, read_tool
    for write_tool in (
        "loom_memory_set",
        "loom_memory_delete",
        "loom_memory_update",
        "loom_memory_pin",
        "loom_blacklist_add",
        "loom_conversation_create",
    ):
        assert write_tool not in names, write_tool


def test_every_readonly_op_tool_is_idempotent_and_viewer_role() -> None:
    """The invariant: nothing a VIEWER could not call appears in the listing.
    Synthetic front-doors (loom_search / loom_activate) are not registry ops
    and are skipped."""
    for name in _tool_names("readonly"):
        op = _TOOL_TO_OP.get(name)
        if op is None:
            continue  # synthetic tool
        assert op.read_only, name


def test_unknown_toolset_falls_back_and_is_not_readonly() -> None:
    # a bad ?toolset= never silently narrows to readonly
    assert _tool_names("bogus") != _tool_names("readonly")


# A viewer may call these and a retry is safe, but each one changes state: an
# auto-approving client must not see them as read-only.
_VIEWER_WRITES = (
    "namespace.join",
    "namespace.leave",
    "namespace.restore",
    "tracker.source.remove",
    "tracker.source.set_mapping",
    "knowledge.synthesize",
    "code.outcome",
    "code.coverage.watch",
    "namespace.mark_seen",
)


@pytest.mark.parametrize("op_name", _VIEWER_WRITES)
def test_an_audited_or_writing_viewer_op_is_not_read_only(op_name: str) -> None:
    from server.transport.mcp_tools import _tool_annotations

    op = next(o for o in ALL_OPERATIONS if o.name == op_name)
    assert op.idempotent and op.required_role == Role.VIEWER
    assert not op.read_only
    assert _tool_annotations(op)["readOnlyHint"] is False
    assert op.mcp_tool_name not in _tool_names("readonly")


def test_read_only_means_no_audit_and_no_write_marker() -> None:
    for op in ALL_OPERATIONS:
        if op.audit or op.writes or op.required_role != Role.VIEWER or not op.idempotent:
            assert not op.read_only, op.name
