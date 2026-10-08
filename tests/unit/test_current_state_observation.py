"""Unit tests for the current-state observation leg.

The leg surfaces the LATEST-dated value of one single-valued attribute as
source="observation", LLM-free via a CH window function. These tests stub the
CH client; the rn=1 (latest-dated) selection is verified at the SQL-probe gate
against loaded data, here we assert the leg's gate, hit shape, and fail-open.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval import (
    SearchHit,
    fetch_current_state_observation,
)


class StubCH:
    def __init__(self, returns: list[dict[str, Any]] | None = None, raise_on_query: bool = False):
        self.returns = returns or []
        self.raise_on_query = raise_on_query
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        if self.raise_on_query:
            raise RuntimeError("cold")
        return self.returns


@pytest.mark.asyncio
async def test_empty_entity_filter_returns_empty() -> None:
    # The caller gates on stated_value_update; an empty entity_filter also
    # short-circuits (no entity to scope the single-valued attribute).
    ch = StubCH(
        returns=[
            {"memory_key": "k", "content_excerpt": "v", "event_date": "2024-01-01", "distance": 0.1}
        ]
    )
    hits = await fetch_current_state_observation(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=[],
    )
    assert hits == []
    # No CH call when there's no entity to restrict on.
    assert ch.calls == []


@pytest.mark.asyncio
async def test_returns_observation_hit_with_current_and_as_of() -> None:
    ch = StubCH(
        returns=[
            {
                "memory_key": "user_current_employer",
                "content_excerpt": "ACME Corp",
                "event_date": "2024-05-01",
                "distance": 0.23,
            }
        ]
    )
    hits = await fetch_current_state_observation(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["employer"],
    )
    assert len(hits) == 1
    h = hits[0]
    assert isinstance(h, SearchHit)
    assert h.source == "observation"
    assert "current" in h.content_excerpt
    assert "as of" in h.content_excerpt
    assert "2024-05-01" in h.content_excerpt
    assert "ACME Corp" in h.content_excerpt
    assert h.valid_at == "2024-05-01"  # label only
    assert abs(h.rank_score - 0.77) < 1e-9


@pytest.mark.asyncio
async def test_sql_uses_row_number_window_latest_dated() -> None:
    # The latest-dated selection is a window function partitioned by the
    # single-valued attribute key, ordered by event date DESC, kept at rn=1.
    ch = StubCH(returns=[])
    await fetch_current_state_observation(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["employer"],
    )
    sql, params = ch.calls[0]
    assert "row_number() OVER" in sql
    assert "PARTITION BY memory_key" in sql
    assert "ORDER BY coalesce(temporal_anchor, valid_at) DESC" in sql
    assert "WHERE rn = 1" in sql
    assert "hasAny(entities, {ents:Array(String)})" in sql
    assert params["ents"] == ["employer"]


@pytest.mark.asyncio
async def test_fail_open_empty_on_ch_error() -> None:
    ch = StubCH(raise_on_query=True)
    hits = await fetch_current_state_observation(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["employer"],
    )
    assert hits == []


@pytest.mark.asyncio
async def test_no_dated_row_returns_empty() -> None:
    ch = StubCH(returns=[])
    hits = await fetch_current_state_observation(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["employer"],
    )
    assert hits == []


@pytest.mark.asyncio
async def test_undated_event_omits_as_of_label() -> None:
    # coalesce sentinel reads as undated; the excerpt then omits the "as of".
    ch = StubCH(
        returns=[
            {
                "memory_key": "user_current_city",
                "content_excerpt": "Berlin",
                "event_date": "1970-01-01",
                "distance": 0.4,
            }
        ]
    )
    hits = await fetch_current_state_observation(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["city"],
    )
    assert hits[0].valid_at == ""
    assert "as of" not in hits[0].content_excerpt
    assert "current" in hits[0].content_excerpt
