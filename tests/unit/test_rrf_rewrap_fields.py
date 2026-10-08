"""Under rrf (memory.search's default mode) the fused pool is re-scored; the
re-scored hit is the same hit with a new score, every other field intact."""

from __future__ import annotations

import dataclasses

from server.memory.app.retrieval.fusion import _rerank_fact_pool
from server.memory.domain.search_types import SearchHit
from tests.unit.test_memory_search_service import _q


def test_rrf_changes_only_the_score() -> None:
    hit = SearchHit(
        memory_key="user_employer",
        memory_type="semantic",
        content_excerpt="works at Acme",
        distance=0.2,
        rank_score=0.8,
        temporal_anchor="2024-01-02",
        temporal_confidence=0.9,
        valid_at="2024-01-02",
        created_at="2024-01-03 10:00:00",
        source="fact",
        verified=True,
        source_resource_id="conv-1",
        entities=["acme"],
        session_id="s1",
        speaker="Caroline",
        about_other=True,
        tags=["speaker:Caroline", "about:other"],
        derived_from=["k0"],
        parent_memory_key="",
        score_components={"semantic": 0.8},
        source_class="extracted",
        status="superseded",
        version=7,
    )
    (fused,) = _rerank_fact_pool([hit], _q(search_mode="rrf", query_text="employer"))
    rescored = {"rank_score", "score_components"}
    for f in dataclasses.fields(SearchHit):
        if f.name not in rescored:
            assert getattr(fused, f.name) == getattr(hit, f.name), f.name
    assert fused.score_components["semantic"] == 0.8
    assert fused.score_components["rrf"] == fused.rank_score
