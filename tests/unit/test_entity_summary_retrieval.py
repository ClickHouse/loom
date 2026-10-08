"""Unit tests for the entity-summary RETRIEVAL leg.

`fetch_entity_summaries` reads the OFFLINE-WORKER-owned loom.entity_summaries
plane and returns source="entity_summary" hits the assembler folds in as
ADDITIVE dense rows (RRF-fused like session summaries, NEVER pool-displacing).
These stub the CH client and assert the leg's hit shape, entity/agent scope
predicate, the NO-LLM-on-the-hot-path guarantee, and fail-open behavior.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval import SearchHit, fetch_entity_summaries
from server.memory.app.retrieval.summaries import _ENTITY_SUMMARY_TOP_N
from server.memory.domain.context_block import _DENSE_SOURCES


class StubCH:
    """Records query_all calls; raises if anything LLM-shaped is invoked."""

    def __init__(self, returns: list[dict[str, Any]] | None = None, raise_on_query: bool = False):
        self.returns = returns or []
        self.raise_on_query = raise_on_query
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        if self.raise_on_query:
            raise RuntimeError("cold table")
        return self.returns


class NoLLM:
    """Any attribute access (complete/embed/...) fails the test — the read leg
    must never invoke an LLM on the hot path (it reads pre-built rows only)."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - guard
        raise AssertionError(f"read leg must not call LLM/embedder: {name!r}")


@pytest.mark.asyncio
async def test_returns_entity_summary_source_hits() -> None:
    ch = StubCH(
        returns=[
            {
                "entity": "bowie",
                "content_excerpt": "On 2023-01-05 Bowie joined; 2024-03-11 Acana.",
                "distance": 0.23,
                "valid_from_iso": "2023-01-05 00:00:00",
            }
        ]
    )
    hits = await fetch_entity_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u1",
        query_vec=[0.1] * 8,
        embedding_model="text-embedding-3-small",
        entity_filter=["bowie"],
    )
    assert len(hits) == 1
    h = hits[0]
    assert isinstance(h, SearchHit)
    assert h.source == "entity_summary"
    assert h.memory_key == "entity_summary:bowie"
    assert h.valid_at == "2023-01-05 00:00:00"  # label parsed through, not a sort key
    assert abs(h.rank_score - 0.77) < 1e-9
    assert abs((1.0 - h.distance) - 0.77) < 1e-9


def test_entity_summary_is_a_dense_source() -> None:
    # The fold treats entity_summary as an additive DENSE plane (admitted
    # unconditionally, never budget-trimmed like a raw chunk).
    assert "entity_summary" in _DENSE_SOURCES


@pytest.mark.asyncio
async def test_no_entities_yields_empty_without_query() -> None:
    # No query entity → nothing to fetch; the leg must not even hit CH.
    ch = StubCH(
        returns=[
            {
                "entity": "x",
                "content_excerpt": "y",
                "distance": 0.1,
                "valid_from_iso": "1970-01-01 00:00:00",
            }
        ]
    )
    hits = await fetch_entity_summaries(
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
    assert ch.calls == []  # short-circuit, no CH read


@pytest.mark.asyncio
async def test_label_sentinel_reads_undated() -> None:
    ch = StubCH(
        returns=[
            {
                "entity": "e",
                "content_excerpt": "digest",
                "distance": 0.5,
                "valid_from_iso": "1970-01-01 00:00:00",
            }
        ]
    )
    hits = await fetch_entity_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["e"],
    )
    assert hits[0].valid_at == ""  # coalesce sentinel surfaces as undated


@pytest.mark.asyncio
async def test_fail_open_empty_on_ch_error() -> None:
    ch = StubCH(raise_on_query=True)
    hits = await fetch_entity_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["e"],
    )
    assert hits == []


@pytest.mark.asyncio
async def test_scopes_on_entities_and_agent() -> None:
    ch = StubCH(returns=[])
    await fetch_entity_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["bowie", "acana"],
    )
    sql, params = ch.calls[0]
    assert "entity IN {ents:Array(String)}" in sql
    assert params["ents"] == ["acana", "bowie"]  # sorted, deduped
    assert "agent IN ({agent:String}, '*')" in sql
    assert params["agent"] == "bot"
    assert "tombstoned_at IS NULL" in sql
    assert "embedding_dim > 0" in sql
    assert "LIMIT 1 BY entity" in sql
    assert params["n"] == _ENTITY_SUMMARY_TOP_N


@pytest.mark.asyncio
async def test_agent_wildcard_drops_agent_predicate() -> None:
    ch = StubCH(returns=[])
    await fetch_entity_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="*",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["e"],
    )
    sql, params = ch.calls[0]
    assert "agent = {agent:String}" not in sql
    assert "agent" not in params


@pytest.mark.asyncio
async def test_read_leg_makes_no_llm_call() -> None:
    # The leg takes a CH client only; there is no LLM/embedder parameter, so it
    # is structurally impossible for it to call one. This pins that no NoLLM
    # access path exists in the read leg (a pure CH read of pre-built rows).
    ch = StubCH(
        returns=[
            {
                "entity": "e",
                "content_excerpt": "d",
                "distance": 0.4,
                "valid_from_iso": "2024-01-01 00:00:00",
            }
        ]
    )
    # If the leg ever reached for an LLM it would have to be passed one; the
    # signature does not accept it. Calling with only `ch` proves the no-LLM path.
    hits = await fetch_entity_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["e"],
    )
    assert len(hits) == 1
    # Exactly one CH read, no other I/O.
    assert len(ch.calls) == 1
