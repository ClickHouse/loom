"""The loom_search response carries the answer — the reading
rule rides once per MCP session, and below-threshold padding is not served."""

from __future__ import annotations

import inspect

import pytest

from server.memory.app.loom_search import fanout as loom_search_fanout
from server.transport import mcp_tools


@pytest.fixture(autouse=True)
def _clean():
    mcp_tools._INSTRUCTED.clear()
    yield
    mcp_tools._INSTRUCTED.clear()


def test_the_rule_is_due_once_per_session_and_always_without_a_session() -> None:
    assert mcp_tools._instructions_due("s1", now=100.0) is True
    assert mcp_tools._instructions_due("s1", now=101.0) is False
    assert mcp_tools._instructions_due("s2", now=101.0) is True
    assert (
        mcp_tools._instructions_due("", now=102.0) is True
        and mcp_tools._instructions_due("", now=103.0) is True
    )
    assert mcp_tools._instructions_due("s1", now=100.0 + mcp_tools._INSTRUCTED_TTL_S + 1) is True, (
        "expired: due again"
    )


def test_the_cap_evicts_the_oldest_tenth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_tools, "_INSTRUCTED_CAP", 20)
    for i in range(20):
        mcp_tools._instructions_due(f"s{i}", now=float(i))
    assert mcp_tools._instructions_due("new", now=50.0) is True
    assert len(mcp_tools._INSTRUCTED) <= 20 and "s0" not in mcp_tools._INSTRUCTED


def test_second_response_carries_the_pointer_not_the_rule() -> None:
    memory = {
        "reader_block": "[2026-09-19] [coffee_type] (semantic) espresso",
        "reader_instructions": "LONG RULE " * 100,
    }
    hits = [{"memory_key": "coffee_type"}]
    first = "\n".join(mcp_tools._memory_section_lines(memory, hits, 5, instructions=True))
    later = "\n".join(mcp_tools._memory_section_lines(memory, hits, 5, instructions=False))
    assert "How to read the memories below: LONG RULE" in first
    assert "LONG RULE" not in later and mcp_tools.READER_RULE_POINTER.strip() in later
    assert len(later) < len(first) / 3  # the real rule is 4,306 chars; the pointer ~180
    assert "[coffee_type]" in later, "the block itself always rides"


def test_the_tool_drops_sub_floor_padding(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.settings import settings

    assert loom_search_fanout.floor_mode() == "drop"
    monkeypatch.setattr(settings, "mcp_loom_search_floor_mode", "bogus")
    assert loom_search_fanout.floor_mode() == "drop", "an unknown value falls back to drop"
    monkeypatch.setattr(settings, "mcp_loom_search_floor_mode", "ids_only")
    assert loom_search_fanout.floor_mode() == "ids_only"
    assert "floor_mode=floor_mode()," in inspect.getsource(loom_search_fanout.search_sections)
    src = inspect.getsource(mcp_tools.search_answer)
    assert "instructions=_due" in src and "_instructions_due(mcp_session_id)" in src
    MemorySearchInput(org="o", namespace="n", agent="*", query="q", floor_mode="drop")


def test_flagged_rows_leave_the_section_when_a_real_hit_exists() -> None:
    """The calibration flags rows under its own bar, above the floor
    `drop` uses; the tool removes them. Their paragraphs leave when
    search_sections renders the block from the final rows."""
    memory = {
        "results": [
            {"memory_key": "coffee_type", "content_excerpt": "espresso"},
            {"memory_key": "fav_place", "content_excerpt": "mountain", "below_threshold": True},
            {"memory_key": "event:b88f", "content_excerpt": "Step 6/6", "below_threshold": True},
        ],
        "reader_block": "[2026-09-19] [coffee_type] (semantic) espresso\n\n[2026-09-18] [fav_place] (semantic) mountain\n\n[2026-09-18] [event:b88f] (semantic) Step 6/6",
        "candidates_total": 20,
    }
    out = loom_search_fanout.drop_flagged_hits(memory)
    assert [h["memory_key"] for h in out["results"]] == ["coffee_type"]
    assert out["below_threshold_dropped"] == 2 and out["candidates_total"] == 20
    # All flagged: an abstain-shaped answer keeps its evidence.
    all_flagged = {
        "results": [{"memory_key": "a", "below_threshold": True}],
        "reader_block": "[d] [a] x",
    }
    assert loom_search_fanout.drop_flagged_hits(all_flagged) is all_flagged
    assert loom_search_fanout.drop_flagged_hits({"results": []}) == {"results": []}
