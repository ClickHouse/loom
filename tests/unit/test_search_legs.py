"""A search reads the legs its caller can show.

`memory.search` takes `legs` (None = every leg); the dashboard's Memories
page passes ["memory"] and the session-summary legs — most of a keystroke's
read bytes and latency — stay out. The field is hidden from the MCP listing (the core
listing budget is a shared ratchet).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.vector_legs import _leg_or_empty, _leg_wanted
from server.transport import mcp_schemas
from tests.unit._memory_search_pkg import handler_source


def _inp(**kw: object) -> MemorySearchInput:
    return MemorySearchInput(org="o", namespace="n", query="q", **kw)  # type: ignore[arg-type]


def test_every_leg_is_wanted_when_the_field_is_unset() -> None:
    inp = _inp()
    assert inp.legs is None
    assert _leg_wanted(inp, "sessions") and _leg_wanted(inp, "memory")


def test_the_memories_page_leaves_the_session_legs_out() -> None:
    inp = _inp(legs=["memory"])
    assert _leg_wanted(inp, "memory")
    assert not _leg_wanted(inp, "sessions")


def test_an_unknown_leg_is_refused() -> None:
    with pytest.raises(ValidationError):
        _inp(legs=["chunks"])


def test_a_caller_without_the_field_at_all_gets_every_leg() -> None:
    class _Old:  # an input model from before the field
        query = "q"

    assert _leg_wanted(_Old(), "sessions")


def test_the_session_leg_consults_the_filter() -> None:
    """The statement itself: the session-summary task is launched only when
    the caller wants that leg; a left-out leg answers [] without a read."""
    src = handler_source()
    launch = next(ln for ln in src.splitlines() if "_summary_task = create_deferred_task(" in ln)
    assert '_leg_or_empty(input, "sessions", _summary_legs)' in launch


def test_legs_is_hidden_from_the_mcp_listing() -> None:
    assert "legs" in mcp_schemas._HIDDEN_OP_PROPS["memory.search"]


@pytest.mark.asyncio
async def test_a_left_out_leg_answers_empty_without_running() -> None:
    ran = []

    async def leg() -> list[str]:
        ran.append(1)
        return ["hit"]

    assert await _leg_or_empty(_inp(legs=["memory"]), "sessions", leg) == []
    assert ran == []
    assert await _leg_or_empty(_inp(), "sessions", leg) == ["hit"]
