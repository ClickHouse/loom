"""A namespace declares its noise floor."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from server.memory.app.search.knobs import _noise_floor


def test_request_floor_wins_else_calibrated(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_min_similarity", 0.22)
    cal = SimpleNamespace(noise_floor=0.31)
    assert _noise_floor(SimpleNamespace(min_similarity=0.40), cal) == 0.40
    assert _noise_floor(SimpleNamespace(min_similarity=None), cal) == pytest.approx(0.31)
    assert _noise_floor(SimpleNamespace(), None) == pytest.approx(0.22)
    assert (
        _noise_floor(SimpleNamespace(min_similarity=0.0), cal) == 0.0
    )  # an explicit zero disables


def test_namespace_defaults_and_request_carry_the_floor() -> None:
    from server.identity.api.models import NamespaceRetrievalDefaults
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import namespace_retrieval_defaults as nrd

    assert "min_similarity" in nrd.FIELDS and "floor_mode" in nrd.FIELDS
    assert nrd.parse('{"min_similarity": 0.4, "floor_mode": "drop"}') == {
        "min_similarity": 0.4,
        "floor_mode": "drop",
    }
    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults(min_similarity=1.5)
    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults(floor_mode="sideways")
    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q", min_similarity=0.4)
    assert inp.min_similarity == 0.4


def test_backfilled_rows_are_not_judged_by_the_floor() -> None:
    from server.memory.app.search.evidence import _apply_min_similarity_floor
    from server.memory.domain.search_types import SearchHit

    def hit(key: str, sim: float, backfill: bool = False) -> SearchHit:
        return SearchHit(
            memory_key=key,
            memory_type="semantic",
            content_excerpt=key,
            distance=1.0 - sim,
            rank_score=sim,
            score_components={"recency_backfill": 1.0} if backfill else {},
        )

    hits = [hit("a", 0.6), hit("newest", 0.3, backfill=True), hit("b", 0.35), hit("c", 0.2)]
    kept = _apply_min_similarity_floor(hits, 0.4, top_k=4, mode="drop")
    assert [h.memory_key for h in kept] == ["a", "newest"]
    # a row the recency pool re-rank scored is exempt too — the ordering is the answer
    pooled = SearchHit(
        memory_key="pooled",
        memory_type="semantic",
        content_excerpt="p",
        distance=0.67,
        rank_score=0.63,
        score_components={"recency_pool": 0.3},
    )
    kept = _apply_min_similarity_floor(
        [hit("a", 0.6), pooled, hit("c", 0.2)], 0.4, top_k=3, mode="drop"
    )
    assert [h.memory_key for h in kept] == ["a", "pooled"]


def test_chunk_max_share_is_a_per_read_knob(monkeypatch) -> None:
    from server.identity.api.models import NamespaceRetrievalDefaults
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import namespace_retrieval_defaults as nrd
    from server.memory.domain.search_types import SearchQuery

    assert "chunk_max_share" in nrd.FIELDS
    assert nrd.parse('{"chunk_max_share": 0.4}') == {"chunk_max_share": 0.4}
    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults(chunk_max_share=2.0)
    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q", chunk_max_share=0.4)
    assert inp.chunk_max_share == 0.4
    q = SearchQuery(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.1],
        top_k=5,
        chunk_max_share=0.4,
    )
    assert q.chunk_max_share == 0.4
