"""`valid_at` on the write path — end-to-end against the running control plane.

`valid_at` records when a fact became true in the world (distinct from
`created_at`, which is when Loom recorded the row). Reads at a valid or a
knowledge time are pinned in `test_bitemporal_memory.py`.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_set_with_valid_at_roundtrips(agent) -> None:
    """`memory.set(valid_at=...)` is preserved and echoed back via get."""
    valid_at = datetime(2023, 6, 1)
    await agent.memory.set("user_location", "NYC", memory_type="semantic", valid_at=valid_at)
    row = await agent.memory.get("user_location")
    assert row is not None
    assert row["value"] == "NYC"
    assert row["valid_at"] == valid_at.isoformat()


@pytest.mark.asyncio
async def test_no_valid_at_reads_none(agent) -> None:
    """Callers that don't pass valid_at read `valid_at` as None."""
    await agent.memory.set("preference", "tabs", memory_type="preference")
    row = await agent.memory.get("preference")
    assert row is not None
    assert row.get("valid_at") is None


@pytest.mark.asyncio
async def test_set_from_messages_extracts_valid_at(agent, require_real_llm) -> None:
    """The extractor emits valid_at for past-tense state changes. Needs the
    server on a real LLM; the fake client runs no extraction prompt."""
    result = await agent.memory.set_from_messages(
        [
            {"role": "user", "content": "I moved to NYC in 2023."},
            {"role": "assistant", "content": "Got it."},
        ],
    )
    assert result["written"], "extractor should emit at least one fact"
    # Inspect the written row(s) — at least one should carry valid_at=2023-*.
    found_2023 = False
    for entry in result["written"]:
        row = await agent.memory.get(entry["memory_key"])
        if row and row.get("valid_at", "").startswith("2023"):
            found_2023 = True
            break
    assert found_2023, "extractor should have anchored at least one fact to 2023"
