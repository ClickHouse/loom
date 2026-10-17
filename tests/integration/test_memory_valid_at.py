"""`valid_at` + `as_of` read paths — end-to-end against the running control
plane. `as_of` is the replay of a server with the bi-temporal journal off (the
default); with it on, reads at a valid or a knowledge time are pinned in
`test_bitemporal_memory.py`.

`valid_at` records when a fact became true in the world (distinct from
`created_at`, which is when Loom recorded the row). The read paths
(`memory.get` / `memory.get_by_id` / `memory.list` / `memory.search`)
accept an optional `as_of` kwarg and filter results via
`coalesce(valid_at, created_at) <= as_of`.

The `valid_at` column lives on both `loom.memory`
and `loom.memory_embeddings` (the latter is the WHERE target for
vector_search; denormalizing avoids a hot-path join).
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
@pytest.mark.no_journal
async def test_get_as_of_filters_by_valid_at(agent) -> None:
    """A row with valid_at=2023-01-01 is visible at as_of=2023-06-01 but
    not at as_of=2022-06-01."""
    await agent.memory.set(
        "user_location",
        "NYC",
        memory_type="semantic",
        valid_at=datetime(2023, 1, 1),
    )
    visible = await agent.memory.get("user_location", as_of=datetime(2023, 6, 1))
    assert visible is not None
    assert visible["value"] == "NYC"

    hidden = await agent.memory.get("user_location", as_of=datetime(2022, 6, 1))
    assert hidden is None


@pytest.mark.asyncio
@pytest.mark.no_journal
async def test_get_as_of_null_valid_at_falls_through_to_created_at(agent) -> None:
    """A row with valid_at=NULL is included when as_of >= created_at and
    excluded when as_of < created_at — the coalesce branch."""
    # Caller doesn't pass valid_at — row writes NULL.
    await agent.memory.set("dialect", "ClickHouse SQL", memory_type="semantic")

    # as_of in the future → row is in window
    visible = await agent.memory.get("dialect", as_of=datetime(2100, 1, 1))
    assert visible is not None
    assert visible["value"] == "ClickHouse SQL"

    # as_of in the past (long before the row was written) → row hidden
    hidden = await agent.memory.get("dialect", as_of=datetime(2000, 1, 1))
    assert hidden is None


@pytest.mark.asyncio
@pytest.mark.no_journal
async def test_list_as_of_excludes_future_rows(agent, ch_client) -> None:
    """memory.list(as_of=X) returns only rows whose
    coalesce(valid_at, created_at) <= X."""
    await agent.memory.set(
        "fact_old",
        "true in 2020",
        memory_type="semantic",
        valid_at=datetime(2020, 1, 1),
    )
    await agent.memory.set(
        "fact_mid",
        "true in 2023",
        memory_type="semantic",
        valid_at=datetime(2023, 1, 1),
    )
    await agent.memory.set(
        "fact_new",
        "true in 2026",
        memory_type="semantic",
        valid_at=datetime(2026, 1, 1),
    )

    result = await agent.memory.list(as_of=datetime(2024, 6, 1))
    keys = {e["memory_key"] for e in result["entries"]}
    assert "fact_old" in keys
    assert "fact_mid" in keys
    assert "fact_new" not in keys


@pytest.mark.asyncio
@pytest.mark.no_journal
async def test_search_as_of_filters_hits(agent) -> None:
    """memory.search(as_of=...) excludes rows whose valid_at > as_of.

    Denormalized valid_at on memory_embeddings is what makes this filter
    apply in the same WHERE pass as the cosine ranking — no join.
    """
    await agent.memory.set(
        "deploy_window_old",
        "Deploy on Tuesday only.",
        memory_type="preference",
        valid_at=datetime(2023, 1, 1),
    )
    await agent.memory.set(
        "deploy_window_new",
        "Deploy on Tuesday or Thursday.",
        memory_type="preference",
        valid_at=datetime(2026, 1, 1),
    )

    # Without as_of, both rows are candidates
    all_hits = await agent.memory.search(
        "when do we deploy",
        memory_type="preference",
        top_k=10,
        auto_touch=False,
    )
    keys = {h.memory_key for h in all_hits}
    # At least one of the two should land — fake-ngram embedder is noisy
    # but the lexical leg catches "deploy" verbatim.
    assert "deploy_window_old" in keys or "deploy_window_new" in keys

    # With as_of=2024, only the 2023 row is in window
    past_hits = await agent.memory.search(
        "when do we deploy",
        memory_type="preference",
        top_k=10,
        auto_touch=False,
        as_of=datetime(2024, 6, 1),
    )
    past_keys = {h.memory_key for h in past_hits}
    assert "deploy_window_new" not in past_keys


@pytest.mark.asyncio
@pytest.mark.no_journal
async def test_get_by_id_honors_as_of(agent) -> None:
    """memory.get_by_id with as_of behaves like memory.get."""
    out = await agent.memory.set(
        "report_v1",
        "Q1 numbers",
        memory_type="semantic",
        valid_at=datetime(2024, 4, 1),
    )
    mid = out["memory_id"]

    # Visible at as_of=2024-06
    row = await agent.memory.get_by_id(mid, as_of=datetime(2024, 6, 1))
    assert row is not None
    assert row["valid_at"] == datetime(2024, 4, 1).isoformat()

    # Hidden at as_of=2024-01 (before the fact became true)
    row2 = await agent.memory.get_by_id(mid, as_of=datetime(2024, 1, 1))
    assert row2 is None


@pytest.mark.asyncio
async def test_no_valid_at_reads_none(agent) -> None:
    """Callers that don't pass valid_at read `valid_at` as None, and reads
    without as_of are unfiltered by it."""
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
