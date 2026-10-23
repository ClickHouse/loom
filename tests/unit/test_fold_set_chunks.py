"""memory.set chunks fold onto their parent fact before the window is cut.

A long memory.set value is also written as verbatim chunks so the chunk plane
can lift the parent fact. Served as rows of their own
(`chunk:memory.set:<key>:<n>`), those chunks would crowd a fact-only window
with chunk ids; folded, a chunk is a path to its fact, not a result.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server.memory.app.retrieval import SearchHit, fusion
from server.memory.app.retrieval import pool as S
from server.memory.app.retrieval.pool import fold_set_chunks


def _hit(
    key: str, score: float, source: str = "fact", *, parent: str = "", excerpt: str = ""
) -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt or key,
        distance=1.0 - score,
        rank_score=score,
        source=source,
        parent_memory_key=parent,
    )


def _set_chunk(parent: str, idx: int, score: float, excerpt: str = "") -> SearchHit:
    return _hit(
        f"chunk:memory.set:{parent}:{idx}",
        score,
        "chunk",
        parent=parent,
        excerpt=excerpt or f"passage {idx} of {parent}",
    )


def test_a_pool_without_set_chunks_is_returned_as_is() -> None:
    hits = [_hit("a", 0.9), _hit("chunk:sess-1:0", 0.8, "chunk"), _hit("b", 0.7)]
    assert fold_set_chunks(hits) is hits


def test_chunk_with_parent_in_pool_merges_and_lifts_the_parent() -> None:
    parent = _hit("lesson_a", 0.60, excerpt="head of the value")
    chunk = _set_chunk("lesson_a", 2, 0.85, excerpt="the matching passage")
    other = _hit("lesson_b", 0.70)
    out = fold_set_chunks([chunk, other, parent])
    assert [h.memory_key for h in out] == ["lesson_a", "lesson_b"]
    folded = out[0]
    assert folded.source == "fact"
    assert folded.rank_score == pytest.approx(0.85)  # the better score
    assert folded.content_excerpt == "the matching passage"  # the passage that matched
    assert folded.distance == pytest.approx(0.15)
    assert folded.score_components["folded_chunks"] == 1.0


def test_a_lower_scored_chunk_keeps_the_parents_excerpt_and_score() -> None:
    parent = _hit("lesson_a", 0.90, excerpt="head")
    chunk = _set_chunk("lesson_a", 0, 0.40, excerpt="tail passage")
    out = fold_set_chunks([parent, chunk])
    assert [h.memory_key for h in out] == ["lesson_a"]
    assert out[0].rank_score == pytest.approx(0.90)
    assert out[0].content_excerpt == "head"
    assert out[0].score_components["folded_chunks"] == 1.0


def test_chunk_without_parent_in_pool_becomes_the_parent_hit() -> None:
    chunk = _set_chunk("lesson_z", 3, 0.75, excerpt="only the tail matched")
    out = fold_set_chunks([_hit("lesson_b", 0.8), chunk])
    assert [h.memory_key for h in out] == ["lesson_b", "lesson_z"]
    z = out[1]
    assert z.source == "fact" and z.parent_memory_key == ""
    assert z.content_excerpt == "only the tail matched"
    assert z.rank_score == pytest.approx(0.75)
    assert z.score_components["folded_from_chunk"] == 3.0
    assert z.score_components["folded_chunks"] == 1.0


def test_several_chunks_of_one_absent_parent_collapse_to_one_row() -> None:
    out = fold_set_chunks(
        [
            _set_chunk("lesson_z", 0, 0.70),
            _set_chunk("lesson_z", 4, 0.80),
            _set_chunk("lesson_z", 1, 0.60),
        ]
    )
    assert [h.memory_key for h in out] == ["lesson_z"]
    assert out[0].rank_score == pytest.approx(0.80)
    assert out[0].content_excerpt == "passage 4 of lesson_z"
    assert out[0].score_components["folded_chunks"] == 3.0


def test_conversation_chunks_and_chunks_without_a_parent_pass_through() -> None:
    conv = _hit("chunk:sess-9:2", 0.9, "chunk")
    orphan = _hit("chunk:memory.set:lesson_q:0", 0.8, "chunk")  # no parent_memory_key
    out = fold_set_chunks([conv, orphan, _hit("lesson_a", 0.5)])
    assert [h.memory_key for h in out] == [
        "chunk:sess-9:2",
        "chunk:memory.set:lesson_q:0",
        "lesson_a",
    ]


def test_order_is_reestablished_by_the_shared_sort_key() -> None:
    out = fold_set_chunks(
        [_hit("lesson_b", 0.70), _set_chunk("lesson_a", 0, 0.95), _hit("lesson_c", 0.80)]
    )
    assert [h.memory_key for h in out] == ["lesson_a", "lesson_c", "lesson_b"]


def test_finish_pool_folds_before_the_chunk_cap_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the fold on, a fact-only namespace under the deployment floor sees
    no chunk ids at all; with it off, the old behaviour stands."""
    from server.settings import settings

    async def no_backfill(ch, q, hits):
        return hits

    monkeypatch.setattr(S, "_recency_backfill", no_backfill)
    monkeypatch.setattr(settings, "memory_search_chunk_min_share", 0.4)
    q = SimpleNamespace(
        top_k=5, recency_intent=False, recency_pool_weight=0.0, chunk_max_share=None
    )
    pool = [_set_chunk(f"lesson_{i}", 0, 0.9 - i * 0.01) for i in range(4)]
    pool += [_hit(f"fact_{i}", 0.6 - i * 0.01) for i in range(6)]

    window = asyncio.run(S._finish_pool(None, list(pool), q))  # type: ignore[arg-type]
    assert [h.memory_key for h in window] == [
        "lesson_0",
        "lesson_1",
        "lesson_2",
        "lesson_3",
        "fact_0",
    ]
    assert all(h.source == "fact" for h in window)


def test_rrf_rewrap_keeps_the_parent_key() -> None:
    """The RRF branch re-scores every hit and must keep parent_memory_key."""
    from tests.unit.test_memory_search_service import _q

    chunk, fact = _set_chunk("lesson_a", 1, 0.5), _hit("lesson_b", 0.4)
    out = {h.memory_key: h for h in fusion._rerank_fact_pool([chunk, fact], _q(search_mode="rrf"))}
    assert (out[chunk.memory_key].source, out[chunk.memory_key].parent_memory_key) == (
        "chunk",
        "lesson_a",
    )
    assert (out["lesson_b"].source, out["lesson_b"].parent_memory_key) == ("fact", "")


def test_the_floor_keeps_a_folded_parents_seat_but_the_cap_does_not_count_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folded parent is the passage match the precision floor protects, so
    the floor promotes it like the chunk it came from; the chunk-share cap
    limits verbatim chunk ROWS, and a folded parent is a fact row — a
    fact-only read keeps it."""
    from server.memory.app.retrieval.pool import cap_chunk_share, floor_chunk_share

    facts = [_hit(f"fact_{i}", 0.9 - i * 0.01) for i in range(6)]
    folded = fold_set_chunks([_set_chunk("lesson_z", 0, 0.30, excerpt="passage")])[0]
    assert folded.source == "fact" and folded.score_components["folded_chunks"] == 1.0
    pool = facts + [folded]  # folded parent ranks last on score
    window = floor_chunk_share(list(pool), 5, 0.4)[:5]
    assert "lesson_z" in [h.memory_key for h in window]  # promoted like a chunk
    capped = cap_chunk_share(list(pool), 5, 0.0)
    assert "lesson_z" in [h.memory_key for h in capped]  # never dropped as a chunk


def test_a_folded_row_is_rank_neutral_no_asserted_tiebreak() -> None:
    """The chunk never had the asserted-fact +0.03 tie-break; the row that
    inherits the chunk's score does not gain it either (with it every folded
    parent would leapfrog the relied-on facts just above it)."""
    from dataclasses import replace

    from server.memory.app.search.scoring import asserted_component

    converted = replace(
        fold_set_chunks([_set_chunk("lesson_z", 0, 0.7)])[0], source_class="asserted"
    )
    assert converted.score_components["folded_lift"] == 1.0
    lifted = replace(
        fold_set_chunks([_hit("lesson_a", 0.5), _set_chunk("lesson_a", 1, 0.8)])[0],
        source_class="asserted",
    )
    assert lifted.score_components["folded_lift"] == 1.0
    kept = replace(
        fold_set_chunks([_hit("lesson_b", 0.9), _set_chunk("lesson_b", 1, 0.4)])[0],
        source_class="asserted",
    )
    assert "folded_lift" not in kept.score_components  # the parent's own score stood
    plain = replace(_hit("lesson_c", 0.6), source_class="asserted")
    n = asserted_component([converted, lifted, kept, plain])
    assert n == 2
    assert "asserted" not in converted.score_components
    assert "asserted" not in lifted.score_components
    assert kept.score_components["asserted"] == 1.0 and plain.score_components["asserted"] == 1.0


def test_a_chunk_folds_onto_its_own_writers_row_of_the_key() -> None:
    mine = replace(_hit("lesson_a", 0.5), identity=("a", "u1", "s1"))
    theirs = replace(_hit("lesson_a", 0.4), identity=("a", "u2", "s2"))
    chunk = replace(_set_chunk("lesson_a", 0, 0.9), identity=("a", "u2", "s2"))
    out = fold_set_chunks([mine, theirs, chunk])
    by_writer = {h.identity: h for h in out}
    assert len(out) == 2
    assert by_writer[("a", "u2", "s2")].rank_score == 0.9  # lifted by its own chunk
    assert by_writer[("a", "u1", "s1")].rank_score == 0.5
