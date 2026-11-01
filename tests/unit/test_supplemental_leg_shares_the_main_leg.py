"""A supplemental leg (sub-query, set-expansion phrase) runs the main leg's query with its own
vector, text and k, and its hits carry what the main leg's hits carry."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search import supplemental
from server.memory.app.search.vector_legs import _search_query_for
from server.memory.domain.search_types import SearchHit


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.5] * 4


def _ctx(**input_kw: Any) -> SimpleNamespace:
    return SimpleNamespace(
        input=MemorySearchInput(org="o", namespace="ns", agent="a", query="main q", **input_kw),
        embedder=_Embedder(),
        actual_work={"embedding_calls": 0, "vector_queries": 0},
        namespaces=["ns"],
        effective_entity_filter=[],
        search_plan=SimpleNamespace(planes=["fact"]),
        graph_expanded_by_ns={},
        graph_paths_by_ns={},
        triple_matched_by_ns={},
        query_anchor_iso="",
        preference_attribute="",
        recency_intent=False,
        chronological_intent="",
        query_vec=[0.1] * 4,
        pool_top_k=30,
        all_hits=[],
    )


async def test_the_leg_runs_the_main_query_and_keeps_every_hit_field(monkeypatch) -> None:
    import server.memory.app.retrieval as ms

    seen: list[Any] = []

    async def search(q: Any) -> list[SearchHit]:
        seen.append(q)
        return [
            SearchHit(
                memory_key="k",
                memory_type="semantic",
                content_excerpt="x",
                distance=0.2,
                rank_score=0.8,
                created_at="2026-09-27 10:00:00",
                derived_from=["src"],
                tags=["t"],
            )
        ]

    monkeypatch.setattr(ms, "vector_search", search)
    ctx = _ctx(key_axis=True, include_tags=True)
    hits = await supplemental._supplemental_leg(ctx, "sub q", 7, set())

    assert seen == [
        replace(_search_query_for(ctx, "ns"), query_vec=[0.5] * 4, top_k=7, query_text="sub q")
    ]
    assert seen[0].key_axis is True
    (hit,) = hits
    # the fresh-hit reread reads created_at; the belt and the wire read the tags
    assert hit.created_at == "2026-09-27 10:00:00"
    assert hit.derived_from == ["src"] and hit.tags == ["t"]
    assert hit.rank_score == pytest.approx(0.8) and hit.namespace == "ns"
