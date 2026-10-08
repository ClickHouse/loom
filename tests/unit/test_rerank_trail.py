"""The reranker trail: what a reranker moved is on
every hit and summarised on the request."""

from __future__ import annotations

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.scoring import _record_rerank_trail


def _hit(key: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=key,
        distance=0.3,
        similarity=0.7,
        rank_score=0.7,
        score_components={},
    )


def test_trail_records_moves_and_summarises_them():
    a, b, c, d = (_hit(k) for k in "abcd")
    before = [a, b, c, d]
    after = [c, a, b, d]  # c promoted two places, a and b demoted one
    summary = _record_rerank_trail(before, after, "external")
    assert summary == {"rerank_moved": 3.0, "rerank_max_move": 2.0}
    assert c.score_components["rerank_from"] == 2.0 and c.score_components["rerank_pos"] == 0.0
    assert (
        c.score_components["rerank_delta"] == 2.0 and c.score_components["rerank_external"] == 1.0
    )
    assert a.score_components["rerank_delta"] == -1.0
    assert d.score_components["rerank_delta"] == 0.0 and "rerank_llm" not in d.score_components


def test_trail_keeps_existing_components_and_handles_identity_order():
    a, b = _hit("a"), _hit("b")
    a.score_components = {"semantic": 0.7}
    summary = _record_rerank_trail([a, b], [a, b], "llm")
    assert summary == {"rerank_moved": 0.0, "rerank_max_move": 0.0}
    assert a.score_components["semantic"] == 0.7 and a.score_components["rerank_llm"] == 1.0
