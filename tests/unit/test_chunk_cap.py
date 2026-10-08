"""Fact rows first — the chunk share cap on the context block."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.domain.context_block import cap_chunk_share, is_chunk_hit


def _hit(key: str, score: float, source: str = "fact") -> SimpleNamespace:
    return SimpleNamespace(memory_key=key, rank_score=score, source=source, distance=1 - score)


def test_is_chunk_hit_by_source_type_or_key() -> None:
    assert is_chunk_hit(_hit("chunk:s0:3", 0.5, "chunk"))
    assert is_chunk_hit(SimpleNamespace(memory_key="x", memory_type="memory_chunk"))
    assert is_chunk_hit(_hit("chunk:s2:1", 0.5, "fact"))
    assert not is_chunk_hit(_hit("nate_recipe", 0.5))


def test_surplus_chunks_give_their_slots_to_the_best_unshown_facts() -> None:
    chunks = [_hit(f"chunk:s0:{i}", 0.9 - i * 0.05, "chunk") for i in range(6)]
    facts_in = [_hit(f"fact_{i}", 0.6 - i * 0.05) for i in range(4)]
    final = sorted(chunks + facts_in, key=lambda h: -h.rank_score)
    unshown = [_hit("gold_recipe", 0.39), _hit("other_fact", 0.38), _hit("period_digest_1_5", 0.5)]
    pool = final + unshown + [_hit("chunk:s9:9", 0.37, "chunk")]
    out, replaced = cap_chunk_share(final, pool, top_k=10, max_share=0.3)
    assert replaced == 2  # six chunks, three allowed, two fact candidates
    keys = [h.memory_key for h in out]
    assert len(out) == 10
    assert sum(1 for h in out if is_chunk_hit(h)) == 4  # replace is bounded by candidates
    assert "gold_recipe" in keys and "other_fact" in keys
    assert "period_digest_1_5" not in keys  # the period slot is its own rule
    assert "chunk:s9:9" not in keys
    # The two lowest-ranked chunks left; the block keeps rank order.
    assert "chunk:s0:5" not in keys and "chunk:s0:4" not in keys
    assert [h.rank_score for h in out] == sorted((h.rank_score for h in out), reverse=True)


def test_no_change_under_the_share_or_without_candidates() -> None:
    final = [_hit("chunk:s0:0", 0.9, "chunk"), _hit("f1", 0.8), _hit("f2", 0.7)]
    assert cap_chunk_share(final, final, top_k=10, max_share=0.3) == (final, 0)
    only_chunks = [_hit(f"chunk:s0:{i}", 0.9 - i * 0.1, "chunk") for i in range(5)]
    assert cap_chunk_share(only_chunks, only_chunks, top_k=5, max_share=0.2) == (only_chunks, 0)
