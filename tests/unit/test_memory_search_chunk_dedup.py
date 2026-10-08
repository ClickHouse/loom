"""Unit tests for the read-side chunk redundancy dedup in the search handler.

`_dedup_redundant_chunks` drops a verbatim chunk whose passage is near-identical
to a higher-ranked fact or chunk already in the rank-ordered pool, trimming the
per-query context-token footprint. Safety: it must never drop a non-chunk hit,
and never drop a chunk that carries an entity/answer the kept item lacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from server.memory.app.search.evidence import _dedup_redundant_chunks


@dataclass
class _Hit:
    content_excerpt: str
    source: str = "fact"
    memory_key: str = "k"
    rank_score: float = 1.0
    score_components: dict = field(default_factory=dict)


def _keys(hits: list[_Hit]) -> list[str]:
    return [h.content_excerpt for h in hits]


def test_near_dup_chunk_dropped_against_higher_ranked_fact() -> None:
    fact = _Hit(
        content_excerpt=("The capital of France is Paris and it sits on the Seine river."),
        source="fact",
        rank_score=2.0,
    )
    chunk = _Hit(
        content_excerpt=(
            "[assistant] The capital of France is Paris and it sits on the Seine river."
        ),
        source="chunk",
        rank_score=1.0,
    )
    out = _dedup_redundant_chunks([fact, chunk])
    assert out == [fact]


def test_near_dup_chunk_dropped_against_higher_ranked_chunk() -> None:
    a = _Hit(
        content_excerpt="[assistant] Jaipur Rugs employs over 40,000 people.",
        source="chunk",
        rank_score=2.0,
    )
    b = _Hit(
        content_excerpt="[assistant] Jaipur Rugs employs over 40,000 people!",
        source="chunk",
        rank_score=1.0,
    )
    out = _dedup_redundant_chunks([a, b])
    assert out == [a]


def test_unique_chunk_kept() -> None:
    a = _Hit(content_excerpt="The capital of France is Paris.", source="fact")
    b = _Hit(
        content_excerpt="[user] My favorite cartoon is Nu pogodi.",
        source="chunk",
    )
    out = _dedup_redundant_chunks([a, b])
    assert _keys(out) == _keys([a, b])


def test_sole_answer_carrier_chunk_never_dropped() -> None:
    # The fact is generic framing; the chunk carries the actual entity. Even
    # though they share boilerplate, the chunk is the only carrier of the
    # answer and must survive.
    fact = _Hit(
        content_excerpt="Here are some recommendations for you to consider.",
        source="fact",
        rank_score=2.0,
    )
    chunk = _Hit(
        content_excerpt=(
            "[assistant] Here are some recommendations: Mindful.org offers "
            "free guided meditation exercises and breathing techniques."
        ),
        source="chunk",
        rank_score=1.0,
    )
    out = _dedup_redundant_chunks([fact, chunk])
    assert chunk in out


def test_facts_never_dropped() -> None:
    # Two near-identical facts: dedup only targets CHUNKS, so both facts stay.
    a = _Hit(content_excerpt="The sky is blue today.", source="fact", rank_score=2.0)
    b = _Hit(content_excerpt="The sky is blue today!", source="fact", rank_score=1.0)
    out = _dedup_redundant_chunks([a, b])
    assert _keys(out) == _keys([a, b])


def test_distinct_numeric_value_facts_never_collapsed() -> None:
    # The de-atomization guard. The shared tokenizer is
    # alpha-only — it STRIPS digits — so "score is 124 points" and "score is
    # 132 points" tokenize identically. If fact-vs-fact dedup ever ran on this
    # signal it would collapse the knowledge-update "124 vs 132 score" lineage
    # into one row. Facts are never dropped here, so
    # both distinct values survive. This is a regression guard against
    # re-introducing number-blind fact dedup.
    a = _Hit(
        content_excerpt="Highest Ticket to Ride score is 124 points.",
        source="fact",
        rank_score=2.0,
    )
    b = _Hit(
        content_excerpt="Highest Ticket to Ride score is 132 points.",
        source="fact",
        rank_score=1.0,
    )
    out = _dedup_redundant_chunks([a, b])
    assert _keys(out) == _keys([a, b])


def test_empty_and_singleton_passthrough() -> None:
    assert _dedup_redundant_chunks([]) == []
    one = [_Hit(content_excerpt="x", source="chunk")]
    assert _dedup_redundant_chunks(one) == one
