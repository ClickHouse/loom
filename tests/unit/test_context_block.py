"""Unit tests for the curated context-block assembler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.memory.domain.context_block import (
    DEFAULT_BLOCK_TOKEN_BUDGET,
    _normalize_summary_scores,
    _tokens_estimate,
    assemble_context_block,
)


@dataclass
class Hit:
    memory_key: str
    content_excerpt: str
    rank_score: float = 0.0
    source: str = "fact"
    valid_at: str = ""
    temporal_anchor: str = ""
    key_facts: list[str] = field(default_factory=list)


def _fact(i: int, *, score: float, chars: int = 200, source: str = "fact") -> Hit:
    body = f"fact{i} " + (f"w{i}x " * (chars // 8))
    return Hit(memory_key=f"k{i}", content_excerpt=body[:chars], rank_score=score, source=source)


def _chunk(i: int, *, score: float, chars: int = 200) -> Hit:
    # Verbatim chunk with no unique signal (no numbers/dates/proper names).
    body = f"chunk{i} " + (f"w{i}x " * (chars // 8))
    return Hit(memory_key=f"c{i}", content_excerpt=body[:chars], rank_score=score, source="chunk")


def test_dense_facts_admitted_unconditionally_even_over_budget() -> None:
    pool = [_fact(i, score=1.0 - i * 0.001, chars=400) for i in range(200)]
    block = assemble_context_block(pool, top_k=120, max_block_tokens=4400)
    assert len(block) == 120
    scores = [h.rank_score for h in block]
    assert scores == sorted(scores, reverse=True)
    used = sum(_tokens_estimate(h.content_excerpt) for h in block)
    assert used > 4400


def test_dense_facts_ignore_tiny_budget() -> None:
    pool = [_fact(i, score=1.0 - i * 0.001, chars=400) for i in range(30)]
    block = assemble_context_block(pool, top_k=20, max_block_tokens=50)
    assert len(block) == 20


def test_signal_free_chunks_trimmed_by_budget() -> None:
    pool = [_chunk(i, score=1.0 - i * 0.001, chars=400) for i in range(200)]
    block = assemble_context_block(pool, top_k=200, max_block_tokens=2000)
    used = sum(_tokens_estimate(h.content_excerpt) for h in block)
    assert len(block) < 200
    assert used <= 2000 + 100


def test_sole_carrier_chunk_survives_past_budget() -> None:
    filler = [_chunk(i, score=0.9 - i * 0.001, chars=400) for i in range(40)]
    carrier = Hit("c_gold", "the user spent $1,247.50 on the new camera lens", 0.10, "chunk")
    pool = [*filler, carrier]
    block = assemble_context_block(pool, top_k=200, max_block_tokens=2000)
    keys = {h.memory_key for h in block}
    assert "c_gold" in keys, "a sole-carrier chunk survives past the budget"


def test_near_duplicate_signal_free_chunk_dropped() -> None:
    base = Hit("c1", "the user enjoys hiking and camping on weekends in summer", 0.90, "chunk")
    near = Hit(
        "c2",
        "the user enjoys hiking and camping on weekends",  # subset
        0.80,
        "chunk",
    )
    other = Hit("c3", "the user prefers tea over coffee in the morning", 0.70, "chunk")
    block = assemble_context_block(
        [base, near, other], top_k=200, max_block_tokens=DEFAULT_BLOCK_TOKEN_BUDGET
    )
    keys = {h.memory_key for h in block}
    assert "c1" in keys and "c3" in keys
    assert "c2" not in keys, "near-duplicate signal-free chunk dropped"


def test_near_duplicate_chunk_with_unique_signal_kept() -> None:
    base = Hit("c1", "the user moved to a new apartment downtown last spring", 0.90, "chunk")
    near_with_date = Hit(
        "c2", "the user moved to a new apartment on 2024-04-15", 0.80, "chunk"
    )  # near-dup prose, but unique date
    block = assemble_context_block(
        [base, near_with_date], top_k=200, max_block_tokens=DEFAULT_BLOCK_TOKEN_BUDGET
    )
    keys = {h.memory_key for h in block}
    assert "c1" in keys and "c2" in keys, "unique date overrides dedup"


def test_tier0_pinned_first_and_survives_tiny_budget() -> None:
    big_facts = [_fact(i, score=0.9 - i * 0.01, chars=400) for i in range(20)]
    obs = Hit("observation:x", "current employer = ACME (as of 2024-03-01)", 0.5, "observation")
    comp = Hit("temporal_diff", "[computed] interval = 30 days", 99.0, "computed")
    pool = [comp, obs, *big_facts]
    block = assemble_context_block(pool, top_k=500, max_block_tokens=50)
    srcs = {h.source for h in block}
    assert "observation" in srcs
    assert "computed" in srcs
    assert block[0].source in {"computed", "observation"}
    assert block[1].source in {"computed", "observation"}


def test_summary_does_not_drop_covered_fact() -> None:
    summary = Hit(
        "summary:s1",
        "On 2024-03-11 the user moved to Berlin and started a new job at ACME.",
        0.95,
        "summary",
        key_facts=["user moved to Berlin", "user started job at ACME"],
    )
    covered_fact = Hit("k_cov", "user moved to Berlin", 0.90, "fact")
    number_fact = Hit("k_num", "user scored 124 on the exam", 0.85, "fact")
    pool = [summary, covered_fact, number_fact]
    block = assemble_context_block(pool, top_k=500, max_block_tokens=DEFAULT_BLOCK_TOKEN_BUDGET)
    keys = {h.memory_key for h in block}
    assert "summary:s1" in keys, "the summary is an additive candidate"
    assert "k_cov" in keys, "a 'covered' fact is NEVER dropped — additive, not aggressive"
    assert "k_num" in keys, "facts are never de-atomized"


def test_exact_duplicate_excerpt_collapses() -> None:
    a = Hit("k1", "user lives in Berlin", 0.90, "fact")
    b = Hit("k2", "user lives in Berlin", 0.80, "fact")  # exact dup, lower rank
    c = Hit("k3", "user works at ACME", 0.70, "fact")
    block = assemble_context_block([a, b, c], top_k=500)
    keys = [h.memory_key for h in block]
    assert "k1" in keys and "k3" in keys
    assert "k2" not in keys, "exact-duplicate excerpt collapses to the higher-ranked"


def test_summary_normalized_into_pool_band() -> None:
    facts = [_fact(0, score=0.030), _fact(1, score=0.010)]
    strong_summary = Hit("s_hi", "alpha", rank_score=0.95, source="summary")
    weak_summary = Hit("s_lo", "beta", rank_score=0.05, source="summary")
    summaries = [strong_summary, weak_summary]
    _normalize_summary_scores(summaries, facts)
    assert 0.010 <= weak_summary.rank_score <= strong_summary.rank_score <= 0.030
    assert strong_summary.rank_score > weak_summary.rank_score


def test_summary_band_reads_the_raw_score_not_the_boosted_one() -> None:
    # One key-match boost lifts a single fact to 1.25 while twenty facts sit at
    # 0.81. The band is the raw band, so a 0.53-cosine summary lands between
    # the raw floor and 0.81 and cannot jump the un-boosted facts.
    @dataclass
    class Boosted(Hit):
        score_components: dict[str, Any] = field(default_factory=dict)

    top = Boosted(
        "k_top",
        "the boosted fact",
        rank_score=1.25,
        score_components={"rank_score_raw": 0.81, "key_match": 0.87},
    )
    facts = (
        [top]
        + [
            Boosted(
                f"k{i}",
                f"fact {i} body",
                rank_score=0.81,
                score_components={"rank_score_raw": 0.81},
            )
            for i in range(1, 6)
        ]
        + [Boosted("k_low", "weak fact", rank_score=0.6, score_components={"rank_score_raw": 0.6})]
    )
    summ = Hit("summary:1", "a session summary", rank_score=0.53, source="summary")
    _normalize_summary_scores([summ], facts)
    assert 0.6 <= summ.rank_score < 0.81
    out = assemble_context_block(facts + [summ], top_k=7)
    assert [h.memory_key for h in out[:6]] == ["k_top", "k1", "k2", "k3", "k4", "k5"]


def test_per_key_cap_frees_slots_for_the_next_key() -> None:
    # Ten rows of one key at 0.81 and two other keys below: with a cap of 3
    # the block holds three of the key and both other keys; with no cap the
    # other keys never make the top five.
    dup = [Hit("user_worktree_path", f"path {i}", rank_score=0.81) for i in range(10)]
    others = [
        Hit("user_task_worktree", "task worktree", rank_score=0.80),
        Hit("user_task_branch", "task branch", rank_score=0.79),
    ]
    out = assemble_context_block(dup + others, top_k=5, per_key_cap=3)
    assert [h.memory_key for h in out] == [
        "user_worktree_path",
        "user_worktree_path",
        "user_worktree_path",
        "user_task_worktree",
        "user_task_branch",
    ]
    out = assemble_context_block(dup + others, top_k=5)
    assert {h.memory_key for h in out} == {"user_worktree_path"}
    # Tier-0 rows are pinned and never counted against the cap.
    obs = [
        Hit("user_worktree_path", f"obs {i}", rank_score=0.1, source="observation")
        for i in range(4)
    ]
    out = assemble_context_block(obs + dup, top_k=6, per_key_cap=1)
    assert [h.source for h in out] == ["observation"] * 4 + ["fact"]


def test_summary_competes_for_top_k_slot() -> None:
    facts = [_fact(i, score=0.030 - i * 0.0001) for i in range(50)]
    strong_summary = Hit("s_hi", "strong digest", rank_score=0.99, source="summary")
    pool = [*facts, strong_summary]
    block = assemble_context_block(pool, top_k=5, max_block_tokens=DEFAULT_BLOCK_TOKEN_BUDGET)
    keys = [h.memory_key for h in block]
    assert "s_hi" in keys, "a strong summary competes into the top slice"


def test_top_k_caps_rows() -> None:
    pool = [_fact(i, score=1.0 - i * 0.001, chars=40) for i in range(100)]
    block = assemble_context_block(pool, top_k=5, max_block_tokens=DEFAULT_BLOCK_TOKEN_BUDGET)
    assert len(block) <= 5


def test_top_k_cap_preserves_tier0_beyond_cap() -> None:
    obs = Hit("observation:x", "current X = Y (as of 2024-01-01)", 0.5, "observation")
    facts = [_fact(i, score=0.9 - i * 0.01, chars=40) for i in range(10)]
    block = assemble_context_block([obs, *facts], top_k=3)
    assert any(h.source == "observation" for h in block)
    assert sum(1 for h in block if h.source != "observation") <= 3


def test_empty_pool_returns_empty() -> None:
    assert assemble_context_block([], top_k=10) == []


def test_wire_hit_type_round_trips() -> None:
    # _MemorySearchHit (the wire type) lacks key_facts; the assembler must cope.
    from server.memory.api.models_search import _MemorySearchHit

    hits: list[Any] = [
        _MemorySearchHit(
            memory_key=f"k{i}",
            memory_type="semantic",
            content_excerpt=f"distinct fact number {i} about topic {i}",
            distance=0.1 + i * 0.001,
            similarity=0.9 - i * 0.001,
            rank_score=0.9 - i * 0.001,
            source="fact",
        )
        for i in range(30)
    ]
    block = assemble_context_block(hits, top_k=10, max_block_tokens=4400)
    assert len(block) <= 10
    assert all(isinstance(h, _MemorySearchHit) for h in block)


def test_per_session_cap_spreads_the_block_across_sessions() -> None:
    """Without a cap, ranks 6–10 can be the same sessions again; with a cap the
    (k+1)-th row of a session yields to the next session in rank order, rows
    without a session_id are never counted, and the block stays full."""
    from dataclasses import dataclass as _dc

    @_dc
    class SHit(Hit):
        session_id: str = ""

    s1 = [
        SHit(f"a{i}", f"session one fact {i}", rank_score=0.90 - i * 0.001, session_id="ns:s1")
        for i in range(8)
    ]
    s2 = [
        SHit(f"b{i}", f"session two fact {i}", rank_score=0.80 - i * 0.001, session_id="ns:s2")
        for i in range(3)
    ]
    s3 = [SHit("c0", "session three fact", rank_score=0.70, session_id="ns:s3")]
    summ = Hit("summary:x", "a digest", rank_score=0.75, source="summary")
    out = assemble_context_block(s1 + s2 + s3 + [summ], top_k=6, per_session_cap=2)
    keys = [h.memory_key for h in out]
    # the summary is band-normalised and may land anywhere in the middle;
    # the facts hold the cap and their rank order
    assert [k for k in keys if k != "summary:x"] == ["a0", "a1", "b0", "b1", "c0"]
    assert "summary:x" in keys and len(keys) == 6
    # no cap: the block is one session
    out = assemble_context_block(s1 + s2 + s3, top_k=6)
    assert {getattr(h, "session_id", "") for h in out} == {"ns:s1"}
    # the cap never leaves the block short when the pool is shallow
    out = assemble_context_block(s1[:2] + s2[:1], top_k=6, per_session_cap=1)
    assert [h.memory_key for h in out] == ["a0", "b0"]


def test_session_of_hit_reads_chunk_and_summary_keys() -> None:
    from server.memory.domain.context_block import session_of_hit

    assert session_of_hit(Hit("chunk:ns-a:session_17:0", "x")) == "ns-a:session_17"
    assert (
        session_of_hit(Hit("summary:ns-a:session_17", "x", source="summary")) == "ns-a:session_17"
    )
    assert session_of_hit(Hit("chunk:conv-abc:3", "x")) == "conv-abc"
    assert session_of_hit(Hit("user_pet_name", "x")) == ""

    from dataclasses import dataclass as _dc

    @_dc
    class SHit(Hit):
        session_id: str = ""

    assert session_of_hit(SHit("user_pet_name", "x", session_id="ns:s4")) == "ns:s4"
    # chunks of one session are capped like facts of one session
    chunks = [
        Hit(f"chunk:ns:session_2:{i}", f"turn {i} words", rank_score=0.9 - i * 0.01)
        for i in range(6)
    ]
    other = [Hit("chunk:ns:session_7:0", "other session", rank_score=0.5)]
    out = assemble_context_block(chunks + other, top_k=4, per_session_cap=2)
    assert [h.memory_key for h in out] == [
        "chunk:ns:session_2:0",
        "chunk:ns:session_2:1",
        "chunk:ns:session_7:0",
    ]
