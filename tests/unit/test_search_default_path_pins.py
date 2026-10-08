"""The memory.search default path, pinned: what production serves with every
ranking experiment off. Each test here describes behaviour that must not move
when an experiment's code is removed.

- A query that cites an entity gets the plain top_k slice: no row carrying
  the cited entity is pulled into the window, the window does not grow.
- A dated question ranks by the point anchor (±anchor_window_days on
  temporal_anchor), not by a question window.
- Graph expansion orders neighbours by their heaviest vote.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.domain.search_types import SearchHit
from tests.unit.test_memory_search_service import StubCH, _hit, _q
from tests.unit.test_search_temporal_prongs import (  # noqa: F401  (fixture re-use)
    _input,
    search_env,
)

_CITING_QUERY = (
    "I am Mary. I'm going on this trip with Jennifer, Eric and Noah. "
    "For dinner on the second day, I'd like to join Noah."
)


def _row(key: str, ents: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key,
        entities=ents,
        source="fact",
        rank_score=1.0,
        content_excerpt=f"trace of {key}",
        namespace="ns",
    )


def test_assembly_serves_the_plain_slice_for_a_cited_entity() -> None:
    from server.memory.domain.context_block import assemble_context_block

    ranked = [
        _row("jennifer", ["jennifer"]),
        _row("eric", ["eric"]),
        _row("mary", ["mary"]),
        _row("noah", ["noah"]),
    ]
    for i, r in enumerate(ranked):
        r.rank_score = 1.0 - 0.1 * i
    out = assemble_context_block(ranked, top_k=2)
    assert [r.memory_key for r in out] == ["jennifer", "eric"]


def _svc(key: str, sim: float, ents: list[str]) -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"{key} trace for the trip",
        distance=1.0 - sim,
        rank_score=sim,
        entities=ents,
    )


@pytest.mark.asyncio
async def test_handler_citing_query_keeps_top_k_and_reports_no_coverage(search_env) -> None:  # noqa: F811
    from server.memory.app.search import handle_memory_search

    search_env.hits = [
        _svc("jennifer_dinner", 0.90, ["jennifer"]),
        _svc("eric_dinner", 0.85, ["eric"]),
        _svc("mary_dinner", 0.80, ["mary"]),
        _svc("noah_dinner", 0.60, ["noah"]),
    ]
    out = await handle_memory_search(
        _input(query=_CITING_QUERY, top_k=2, entity_filter=["noah", "jennifer", "eric"])
    )
    served = [
        h.memory_key for h in out.results if h.memory_key in {x.memory_key for x in search_env.hits}
    ]
    assert served == ["jennifer_dinner", "eric_dinner"]
    for k in ("actor_coverage", "entity_coverage_filled", "window_per_cited_entity"):
        assert k not in out.retrieval_work


def _fact_sql(ch: StubCH) -> str:
    calls = [c for c in ch.query_all_calls if "loom.memory_embeddings AS e" in c[0]]
    assert calls
    return calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["temporal"])
async def test_dated_question_ranks_by_the_point_anchor(mode: str, monkeypatch) -> None:
    from server.core.vectors import patch_parts
    from server.memory.app.retrieval import vector_search
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_key_axis", "off")
    patch_parts.reset()
    ch = StubCH()
    ch.query_all_returns.append([_hit()])
    await vector_search(
        _q(
            search_mode=mode,
            execution_planes=["fact", "temporal"],
            query_anchor_iso="2023-05-05",
            anchor_window_days=7,
        ),
        ch=ch,
    )
    sql = _fact_sql(ch)
    assert "parseDateTimeBestEffortOrNull({query_anchor:String})" in sql
    assert "<= {anchor_window:UInt32}" in sql
    assert "qw_start" not in sql and "qw_end" not in sql
    patch_parts.reset()


class _GraphCH:
    def __init__(self, hops: list[list[dict[str, Any]]]) -> None:
        self.hops = hops

    async def query_all(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM loom.entity_degree" in sql:
            return []
        return self.hops.pop(0) if self.hops else []


@pytest.mark.asyncio
async def test_graph_expansion_orders_by_the_heaviest_vote() -> None:
    from server.memory.app.retrieval import fetch_graph_neighbors

    hop1 = [
        {"neighbor": "hub", "weight": 2, "via_seed": "alice"},
        {"neighbor": "multi", "weight": 2, "via_seed": "alice"},
        {"neighbor": "x", "weight": 1, "via_seed": "bob"},
    ]
    paths: dict[str, list[str]] = {}
    out = await fetch_graph_neighbors(
        _GraphCH([hop1]),
        org="o",
        namespace="n",
        seed_entities=["alice", "bob"],
        hops=1,
        hub_skip_degree=0,
        max_neighbors=3,
        paths=paths,
    )
    assert out == ["hub", "multi", "x"]
    assert paths["multi"] == ["alice", "multi"] and paths["x"] == ["bob", "x"]
