"""The verbatim-chunk leg's share of a search window."""

from __future__ import annotations

from server.memory.app.retrieval import SearchHit, chunk_share
from server.memory.app.retrieval.pool import cap_chunk_share


def _hit(key: str, score: float, source: str) -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=key,
        distance=1.0 - score,
        rank_score=score,
        source=source,
    )


def _mixed() -> list[SearchHit]:
    # nine chunks out-ranking three facts, as the live LoCoMo windows do
    hits = [_hit(f"chunk:ns:s{i}:0", 0.9 - i * 0.01, "chunk") for i in range(9)]
    hits += [_hit(f"user_fact_{i}", 0.7 - i * 0.01, "fact") for i in range(3)]
    return hits


def test_no_cap_is_a_no_op() -> None:
    hits = _mixed()
    assert cap_chunk_share(hits, 10, 1.0) == hits
    assert chunk_share(hits, 10) == 0.9


def test_half_share_lets_the_facts_into_the_window() -> None:
    out = cap_chunk_share(_mixed(), 10, 0.5)
    window = out[:10]
    kinds = [h.source for h in window]
    assert kinds.count("chunk") == 7  # five allowed, then chunks fill the two empty slots
    assert kinds.count("fact") == 3
    assert window[0].memory_key == "chunk:ns:s0:0"
    assert [h.memory_key for h in window if h.source == "fact"] == [
        "user_fact_0",
        "user_fact_1",
        "user_fact_2",
    ]
    assert len(out) == 12 and out[10].source == "chunk"
    assert chunk_share(out, 10) == 0.7


def test_all_chunks_stay_all_chunks() -> None:
    hits = [_hit(f"chunk:ns:s{i}:0", 0.9 - i * 0.01, "chunk") for i in range(6)]
    assert cap_chunk_share(hits, 10, 0.5) == hits
    assert chunk_share(hits, 10) == 1.0


def test_chunk_share_reads_the_key_prefix_too() -> None:
    hits = [_hit("chunk:ns:s1:0", 0.9, "fact"), _hit("user_x", 0.8, "fact")]
    assert chunk_share(hits, 2) == 0.5
    assert chunk_share([], 10) == 0.0


# --- the floor, and chunk parity in the lexical modes ---------

from server.memory.app.retrieval.chunks import chunk_rank_score  # noqa: E402
from server.memory.app.retrieval.pool import floor_chunk_share  # noqa: E402


def _facts_over_chunks() -> list[SearchHit]:
    # a bench bm25_hybrid shape: 25 reinforced one-line facts above 10 chunks
    hits = [_hit(f"user_fact_{i}", 1.3 - i * 0.01, "fact") for i in range(25)]
    hits += [_hit(f"chunk:ns:s{i}:0", 0.7 - i * 0.01, "chunk") for i in range(10)]
    return hits


def test_floor_is_a_no_op_at_zero_and_when_the_window_already_has_enough():
    hits = _facts_over_chunks()
    assert floor_chunk_share(hits, 25, 0.0) is hits
    mixed = _mixed()
    assert [h.memory_key for h in floor_chunk_share(mixed, 12, 0.4)] == [
        h.memory_key for h in mixed
    ]


def test_floor_promotes_the_best_chunks_and_displaces_the_lowest_facts():
    out = floor_chunk_share(_facts_over_chunks(), 25, 0.4)
    window = out[:25]
    chunks = [h for h in window if h.source == "chunk"]
    assert len(chunks) == 10  # int(25 * 0.4)
    # order kept inside each group: best chunks first, best facts first
    assert [h.memory_key for h in chunks] == [f"chunk:ns:s{i}:0" for i in range(10)]
    assert [h.memory_key for h in window[:15]] == [f"user_fact_{i}" for i in range(15)]
    # displaced facts follow the window in rank order, then the rest
    assert [h.memory_key for h in out[25:35]] == [f"user_fact_{i}" for i in range(15, 25)]
    assert len(out) == 35


def test_floor_keeps_every_chunk_when_the_pool_has_fewer_than_the_share():
    hits = _facts_over_chunks()[:27]  # 25 facts + 2 chunks
    window = floor_chunk_share(hits, 25, 0.4)[:25]
    assert sum(1 for h in window if h.source == "chunk") == 2


def test_chunk_rank_default_shape_is_unchanged():
    assert chunk_rank_score(0.4, 0.05, 0.02) == 0.6 + 0.05 + 0.02


def test_chunk_rank_parity_matches_the_fact_leg_shape():
    # alpha 0.5, half the query tokens present, cosine 0.6 -> 0.5*0.5 + 0.5*0.6
    assert abs(chunk_rank_score(0.4, 0.0, 0.0, lexical=0.5, alpha=0.5, parity=True) - 0.55) < 1e-9
    # a chunk carrying every token of an identifier query out-ranks its cosine alone
    assert chunk_rank_score(0.4, 0.0, 0.0, lexical=1.0, alpha=0.5, parity=True) > 0.6
    # parity off ignores the lexical share
    assert chunk_rank_score(0.4, 0.0, 0.0, lexical=1.0, alpha=0.5, parity=False) == 0.6


def test_zero_cap_drops_chunks_even_when_nothing_else_fills_the_window() -> None:
    """A fact-only read (chunk_max_share=0) never hands back a chunk id: with no
    fact to take the slot, backfilling the window with the chunk it had just
    deferred would break the contract. Fewer rows than top_k is the honest
    answer."""
    only_chunks = [_hit(f"chunk:memory.set:l{i}:0", 0.9 - i * 0.01, "chunk") for i in range(4)]
    assert cap_chunk_share(only_chunks, 10, 0.0) == []
    mixed = _mixed()
    out = cap_chunk_share(mixed, 10, 0.0)
    assert out and all(h.source != "chunk" for h in out)
    assert [h.memory_key for h in out] == [h.memory_key for h in mixed if h.source != "chunk"]
    # a small positive cap keeps displace-and-backfill semantics
    assert len(cap_chunk_share(only_chunks, 10, 0.1)) == 4
