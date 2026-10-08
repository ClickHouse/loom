"""Handler-stage fusion / rerank / budget helpers — pure functions."""

from __future__ import annotations

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.budget import SearchBudget
from server.memory.app.search.evidence import (
    _apply_min_similarity_floor,
    _derived_evidence_hit,
    _merge_namespace_scope,
)
from server.memory.app.search.scoring import (
    _anchor_rank_score,
    _effective_ann_boosts,
)
from server.memory.app.search.telemetry import _actual_work_payload


def _hit(
    key: str,
    *,
    rank_score: float,
    distance: float = 0.4,
    ann_boost: float = 0.0,
    sql_entity_boost: float = 0.0,
    excerpt: str = "",
) -> _MemorySearchHit:
    comps: dict[str, float] = {"rank_score_raw": rank_score}
    if ann_boost:
        comps["entity_ann_boost"] = ann_boost
    if sql_entity_boost:
        comps["entity_boost_sql"] = sql_entity_boost
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt or f"content for {key}",
        distance=distance,
        similarity=1.0 - distance,
        rank_score=rank_score,
        score_components=comps,
    )


def test_ann_boost_cannot_exceed_pool_max_on_rrf_pool() -> None:
    pool = [
        _hit("answer", rank_score=2 / 61),
        _hit("weak_entity_row", rank_score=2 / 64, ann_boost=0.4),
    ]
    boosts = _effective_ann_boosts(pool, rrf_pool=True)
    pool_max = max(h.rank_score for h in pool)
    assert boosts, "boosted row must receive a contribution"
    assert max(boosts.values()) <= pool_max
    assert max(boosts.values()) == pytest.approx(1.0 / 61.0)
    assert 0 not in boosts


def test_ann_boost_rrf_arm_ranks_by_boost_strength() -> None:
    pool = [
        _hit("strong_boost", rank_score=0.030, ann_boost=0.45),
        _hit("weak_boost", rank_score=0.031, ann_boost=0.10),
        _hit("no_boost", rank_score=0.032),
    ]
    boosts = _effective_ann_boosts(pool, rrf_pool=True)
    assert boosts[0] == pytest.approx(1.0 / 61.0)
    assert boosts[1] == pytest.approx(1.0 / 62.0)
    assert 2 not in boosts


def test_ann_boost_clamped_to_pool_ceiling_on_additive_pool() -> None:
    weak_pool = [
        _hit("a", rank_score=0.30),
        _hit("b", rank_score=0.20, ann_boost=0.5),
    ]
    boosts = _effective_ann_boosts(weak_pool, rrf_pool=False)
    assert boosts[1] == pytest.approx(0.30)  # clamped to pool max

    strong_pool = [
        _hit("a", rank_score=1.5),
        _hit("b", rank_score=1.0, ann_boost=0.4),
    ]
    boosts2 = _effective_ann_boosts(strong_pool, rrf_pool=False)
    assert boosts2[1] == pytest.approx(0.4)  # below ceiling, untouched


def test_ann_boost_attenuated_when_sql_entity_boost_already_fired() -> None:
    pool = [
        _hit("a", rank_score=1.5),
        _hit("double_counted", rank_score=1.0, ann_boost=0.4, sql_entity_boost=0.5),
        _hit("ann_only", rank_score=1.0, ann_boost=0.4),
    ]
    boosts = _effective_ann_boosts(pool, rrf_pool=False)
    assert boosts[1] == pytest.approx(0.2)  # halved
    assert boosts[2] == pytest.approx(0.4)  # full


def test_ann_boost_empty_and_unboosted_pools() -> None:
    assert _effective_ann_boosts([], rrf_pool=True) == {}
    pool = [_hit("a", rank_score=0.5), _hit("b", rank_score=0.4)]
    assert _effective_ann_boosts(pool, rrf_pool=False) == {}


def test_anchor_rows_land_below_pool_min() -> None:
    pool_floor = 0.0163  # rrf-scale pool minimum
    for sim in (0.0, 0.3, 0.7, 0.99, 1.0):
        assert _anchor_rank_score(sim, pool_floor) < pool_floor


def test_anchor_rows_ordered_by_similarity_below_floor() -> None:
    pool_floor = 0.5
    strong = _anchor_rank_score(0.8, pool_floor)
    weak = _anchor_rank_score(0.3, pool_floor)
    assert weak < strong < pool_floor


def test_anchor_rows_below_negative_pool_floor() -> None:
    assert _anchor_rank_score(0.9, -0.2) < -0.2


def test_floor_exempts_pools_smaller_than_top_k() -> None:
    hits = [
        _hit("a", rank_score=0.5, distance=0.6),  # sim 0.40
        _hit("b", rank_score=0.4, distance=0.75),  # sim 0.25 < floor
        _hit("c", rank_score=0.3, distance=0.80),  # sim 0.20 < floor
    ]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=10)
    assert kept == hits


def test_floor_fills_explicit_top_k_instead_of_deleting() -> None:
    hits = [
        _hit("a", rank_score=0.5, distance=0.6),  # sim 0.40
        _hit("b", rank_score=0.4, distance=0.75),  # sim 0.25 < floor
    ]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=2)
    assert [h.memory_key for h in kept] == ["a", "b"]
    assert "below_floor" not in kept[0].score_components
    assert kept[1].score_components["below_floor"] == 1.0


def test_floor_fill_keeps_confident_rows_first_in_score_order() -> None:
    hits = [
        _hit("weak_top", rank_score=0.9, distance=0.76),  # sim 0.24 < floor
        _hit("strong", rank_score=0.8, distance=0.5),  # sim 0.50
        _hit("weak_2", rank_score=0.7, distance=0.78),  # sim 0.22 < floor
        _hit("weak_3", rank_score=0.6, distance=0.79),  # sim 0.21 < floor
    ]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=4)
    assert [h.memory_key for h in kept] == ["strong", "weak_top", "weak_2", "weak_3"]


def test_floor_fill_is_capped_at_requested_top_k() -> None:
    hits = [
        _hit("a", rank_score=0.9, distance=0.5),  # sim 0.50 — survives
        _hit("b", rank_score=0.8, distance=0.75),  # sim 0.25 — filler 1
        _hit("c", rank_score=0.7, distance=0.78),  # sim 0.22 — filler 2 (cut)
    ]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=2)
    assert [h.memory_key for h in kept] == ["a", "b"]


def test_floor_fill_never_admits_zero_similarity_noise() -> None:
    hits = [
        _hit("a", rank_score=0.9, distance=0.5),  # sim 0.50
        _hit("noise", rank_score=0.8, distance=1.0),  # sim 0.00
    ]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=2)
    assert [h.memory_key for h in kept] == ["a"]


def test_floor_fill_when_nothing_survives_keeps_above_zero_pool() -> None:
    hits = [
        _hit("a", rank_score=0.9, distance=0.72),  # sim 0.28
        _hit("b", rank_score=0.8, distance=0.75),  # sim 0.25
    ]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=2)
    assert [h.memory_key for h in kept] == ["a", "b"]
    assert all(h.score_components["below_floor"] == 1.0 for h in kept)


def test_floor_without_top_k_keeps_legacy_behavior() -> None:
    hits = [_hit("a", rank_score=0.5, distance=0.9)]  # sim 0.10
    assert _apply_min_similarity_floor(hits, 0.30) == []
    # floor=0 disables filtering entirely.
    assert _apply_min_similarity_floor(hits, 0.0, top_k=5) == hits


def test_derived_evidence_count_row_pins_above_pool() -> None:
    pool = [_hit("a", rank_score=2.4), _hit("b", rank_score=1.1)]
    agg = {
        "op": "count",
        "count": 3,
        "items": [
            {"object": "vase", "numeric_value": 2000.0, "unit": "usd", "occurred_at": "2023-05-24"},
            {
                "object": "first-edition book",
                "numeric_value": 800.0,
                "unit": "usd",
                "occurred_at": "2023-06-01",
            },
        ],
    }
    row = _derived_evidence_hit(agg, pool, namespace="ns1")
    assert row.source == "derived"
    assert row.memory_key == "derived_aggregate"
    assert row.namespace == "ns1"
    assert row.rank_score > max(h.rank_score for h in pool)
    assert row.similarity == 1.0
    assert row.content_excerpt.startswith("Computed from 3 dated records: count = 3")
    assert "vase (2000 usd, 2023-05-24)" in row.content_excerpt
    assert row.score_components["derived_aggregate"] == 1.0
    assert row.explanation


def test_derived_evidence_sum_renders_currency_total() -> None:
    agg = {
        "op": "sum",
        "count": 2,
        "sum": 185.0,
        "items": [
            {"object": "helmet", "numeric_value": 120.0, "unit": "usd", "occurred_at": "2024-01-05"}
        ],
    }
    row = _derived_evidence_hit(agg, [], namespace="ns")
    assert "total = $185" in row.content_excerpt
    assert row.rank_score > 0.0  # above an empty pool's max


def test_derived_evidence_date_span_renders_first_and_last() -> None:
    from datetime import datetime

    agg = {
        "op": "date_span",
        "count": 2,
        "span_days": 10.0,
        "first_at": datetime(2023, 3, 15),
        "last_at": datetime(2023, 3, 25),
        "items": [],
    }
    row = _derived_evidence_hit(agg, [], namespace="ns")
    assert "span = 10 days (first 2023-03-15, last 2023-03-25)" in row.content_excerpt


def test_derived_evidence_flags_broadened_category() -> None:
    agg = {"op": "count", "count": 1, "items": [], "category_broadened": True}
    row = _derived_evidence_hit(agg, [], namespace="ns")
    assert "[category filter broadened]" in row.content_excerpt


def test_all_mine_always_includes_explicit_namespace() -> None:
    assert _merge_namespace_scope("mine", ["other-grant"]) == ["mine", "other-grant"]


def test_all_mine_no_duplicate_when_already_granted() -> None:
    assert _merge_namespace_scope("mine", ["team", "mine"]) == ["team", "mine"]


def test_all_mine_falls_back_to_explicit_when_no_grants() -> None:
    assert _merge_namespace_scope("mine", []) == ["mine"]


def test_all_mine_drops_empty_grant_entries() -> None:
    assert _merge_namespace_scope("mine", ["", "team"]) == ["mine", "team"]


def test_note_llm_call_meters_without_consuming_fallback_budget() -> None:
    b = SearchBudget(max_rounds=1, max_wall_ms=60_000, max_llm_calls=2)
    for _ in range(5):
        b.note_llm_call(1)
    assert b.llm_calls_metered == 5
    # Metering never denies and never consumes the rationed counters.
    assert b.llm_calls_used == 0
    assert b.rounds_used == 0
    assert not b.exhausted
    assert b.try_spend_fallback(rounds=1, llm_calls=1) is True


def test_judge_split_spend_refunds_round_on_approval() -> None:
    b = SearchBudget(max_rounds=1, max_wall_ms=60_000, max_llm_calls=2)
    # Judge admission spends the LLM unit only.
    assert b.try_spend_llm(1) is True
    assert b.rounds_used == 0
    # Approval spends no round; the HyDE leg can still fire.
    assert b.try_spend_fallback(rounds=1, llm_calls=1, vector_queries=1) is True
    assert b.rounds_used == 1


def test_actual_work_payload_reports_total_llm_calls() -> None:
    b = SearchBudget(max_rounds=1, max_wall_ms=60_000)
    b.note_llm_call(3)
    b.try_spend_llm(1)
    payload = _actual_work_payload({}, b)
    assert payload["llm_calls_total"] == 3
    assert payload["fallback_llm_calls_used"] == 1


def test_remaining_wall_s_floors_at_point_one() -> None:
    b = SearchBudget(max_wall_ms=0)
    assert b.remaining_wall_s() == pytest.approx(0.1)
    b2 = SearchBudget(max_wall_ms=60_000)
    assert 0.1 < b2.remaining_wall_s() <= 60.0


def test_search_budget_plan_cache_isolated_per_instance():
    a, b = SearchBudget(), SearchBudget()
    a.plan_cache[("enrich", "q", "")] = ("cached",)
    assert b.plan_cache == {}
    assert a.plan_cache[("enrich", "q", "")] == ("cached",)


# ---------------------------------------------------------------------------
# The RRF rank orders are total, so the
# fused scores do not depend on the order the legs returned the candidates.
# ---------------------------------------------------------------------------


def test_rrf_scores_are_a_function_of_the_hits_not_their_arrival_order() -> None:
    import random
    from types import SimpleNamespace

    from server.memory.app.retrieval.fusion import rrf_scores

    # a cold conversational namespace: most candidates carry lex_score 0
    hits = [
        SimpleNamespace(memory_key=f"k{i:02d}", rank_score=0.0, distance=0.30 + 0.01 * i)
        for i in range(12)
    ]
    hits += [
        SimpleNamespace(memory_key="lex_a", rank_score=2.5, distance=0.62),
        SimpleNamespace(memory_key="lex_b", rank_score=2.5, distance=0.41),
    ]
    base = dict(zip((h.memory_key for h in hits), rrf_scores(hits, w_lex=1.0), strict=True))
    rng = random.Random(7)
    for _ in range(25):
        perm = hits[:]
        rng.shuffle(perm)
        got = dict(zip((h.memory_key for h in perm), rrf_scores(perm, w_lex=1.0), strict=True))
        assert got == base
    # tied lexical scores rank by distance: the nearer lex_b outranks lex_a on the lexical leg too
    assert base["lex_b"] > base["lex_a"]
    # and among the lex_score-0 crowd the nearest wins on both legs
    assert base["k00"] > base["k11"]
