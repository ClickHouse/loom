"""Unit tests for server/memory/app/search/ann_entity_boosts.py.

The lookup's job is subtler than "nearest 20 rows". `loom.entity_vectors` stores
one row per (entity_text, memory_key) EDGE, with the entity's vector copied onto
every edge, so a flat top-N over that table returns N edges rather than N
entities: a flat `LIMIT 20` can surface a handful of distinct entities, or spend
the whole budget on a single well-connected one. These tests pin the two-stage
shape that avoids it, and the fan-out attenuation that keeps hub entities from
dominating.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.search.ann_entity_boosts import _ann_entity_boosts


class _StubCH:
    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows


class _StubEmbedder:
    dim = 4
    model_name = "stub"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]


def _patch_client(monkeypatch, ch) -> None:
    monkeypatch.setattr("server.memory.app.search.ann_entity_boosts.get_client", lambda: ch)


def _row(memory_key: str, distance: float, namespace: str = "ns") -> dict[str, Any]:
    return {"namespace": namespace, "memory_key": memory_key, "distance": distance}


@pytest.mark.asyncio
async def test_ranks_distinct_entities_then_expands_to_edges(monkeypatch) -> None:
    """The SQL must rank distinct entities first, not raw edges.

    Without the GROUP BY stage, one hub entity's edges fill the whole candidate
    budget and closer-but-less-connected entities never surface at all.
    """
    ch = _StubCH([_row("m-1", 0.1)])
    _patch_client(monkeypatch, ch)
    await _ann_entity_boosts(
        org="o",
        namespaces=["ns"],
        agent="a",
        user_id="u",
        query_entities=["Alice"],
        embedder=_StubEmbedder(),
    )
    sql, _ = ch.calls[0]
    assert "GROUP BY entity_text" in sql, "lost the distinct-entity ranking stage"
    assert "ORDER BY min(" in sql, "entities must rank by their BEST edge distance"
    assert "entity_text IN (SELECT entity_text FROM top_ents)" in sql


@pytest.mark.asyncio
async def test_candidate_and_pair_bounds_come_from_settings(monkeypatch) -> None:
    from server.settings import settings

    ch = _StubCH([])
    _patch_client(monkeypatch, ch)
    await _ann_entity_boosts(
        org="o",
        namespaces=["ns"],
        agent="a",
        user_id="u",
        query_entities=["Alice"],
        embedder=_StubEmbedder(),
    )
    sql, _ = ch.calls[0]
    assert f"LIMIT {settings.memory_search_entity_boost_top_entities}" in sql
    # The pair cap is what keeps a hub entity (461 edges on one live probe) from
    # dragging unbounded work into the aggregation.
    assert f"LIMIT {settings.memory_search_entity_boost_max_pairs}" in sql
    assert settings.memory_search_entity_boost_max_pairs > (
        settings.memory_search_entity_boost_top_entities
    )


@pytest.mark.asyncio
async def test_similarity_floor_drops_weak_edges(monkeypatch) -> None:
    """Only edges at similarity >= 0.5 contribute (distance <= 0.5)."""
    ch = _StubCH([_row("keep", 0.2), _row("drop", 0.9)])
    _patch_client(monkeypatch, ch)
    out = await _ann_entity_boosts(
        org="o",
        namespaces=["ns"],
        agent="a",
        user_id="u",
        query_entities=["Alice"],
        embedder=_StubEmbedder(),
    )
    assert ("ns", "keep") in out
    assert ("ns", "drop") not in out


@pytest.mark.asyncio
async def test_hub_entity_is_attenuated_below_a_narrow_one(monkeypatch) -> None:
    """Fan-out attenuation: many edges from one query entity = weaker signal.

    This is the property that makes the wider candidate set safe. The two-stage
    lookup lets `spread` see the true edge count instead of a value truncated at
    20, so a hub entity is attenuated in proportion to its real fan-out.
    """
    narrow = _StubCH([_row("m-1", 0.1)])
    _patch_client(monkeypatch, narrow)
    tight = await _ann_entity_boosts(
        org="o",
        namespaces=["ns"],
        agent="a",
        user_id="u",
        query_entities=["Alice"],
        embedder=_StubEmbedder(),
    )

    wide = _StubCH([_row(f"m-{i}", 0.1) for i in range(200)])
    _patch_client(monkeypatch, wide)
    spread = await _ann_entity_boosts(
        org="o",
        namespaces=["ns"],
        agent="a",
        user_id="u",
        query_entities=["Alice"],
        embedder=_StubEmbedder(),
    )

    # attenuation = 1 / (1 + 0.001 * (spread - 1)^2), so spread=200 gives
    # 1/(1 + 0.001*199^2) ~= 0.0246 against 1.0 for a single edge — a ~40x gap.
    ratio = tight[("ns", "m-1")] / spread[("ns", "m-1")]
    assert 30 < ratio < 60, f"expected a ~40x attenuation gap for a 200-edge hub, got {ratio:.1f}x"


@pytest.mark.asyncio
async def test_boost_is_capped_per_key(monkeypatch) -> None:
    """No single key exceeds 0.5, so entity boost can't dominate rank_score."""
    ch = _StubCH([_row("m-1", 0.0)])
    _patch_client(monkeypatch, ch)
    out = await _ann_entity_boosts(
        org="o",
        namespaces=["ns"],
        agent="a",
        user_id="u",
        query_entities=["Alice", "Bob", "Carol", "Dave"],
        embedder=_StubEmbedder(),
    )
    assert out[("ns", "m-1")] <= 0.5


@pytest.mark.asyncio
async def test_fails_open_on_clickhouse_error(monkeypatch) -> None:
    """The boost is additive; a CH failure must not fail the search."""
    _patch_client(monkeypatch, _StubCH(RuntimeError("CH down")))
    out = await _ann_entity_boosts(
        org="o",
        namespaces=["ns"],
        agent="a",
        user_id="u",
        query_entities=["Alice"],
        embedder=_StubEmbedder(),
    )
    assert out == {}


@pytest.mark.asyncio
async def test_no_query_entities_skips_clickhouse_entirely(monkeypatch) -> None:
    ch = _StubCH([])
    _patch_client(monkeypatch, ch)
    out = await _ann_entity_boosts(
        org="o",
        namespaces=["ns"],
        agent="a",
        user_id="u",
        query_entities=[],
        embedder=_StubEmbedder(),
    )
    assert out == {}
    assert ch.calls == []
