"""A hand-asserted fact wins a near-tie against a derived row.
The boost rides the key-match arm's invariant: at most the scale, never
past the pool's strongest raw score; system-shaped keys and procedures are not
a person's fact and earn nothing."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from server.memory.app.search.scoring import _effective_arm_boosts, asserted_component


def _hit(key, raw, *, source="fact", source_class="", memory_type="semantic"):
    return SimpleNamespace(
        memory_key=key,
        rank_score=raw,
        source=source,
        source_class=source_class,
        memory_type=memory_type,
        score_components={"rank_score_raw": raw},
    )


def test_asserted_fact_gets_the_tiebreak_and_derived_rows_do_not() -> None:
    hits = [
        _hit("user_favorite_place", 0.409, source_class="extracted"),
        _hit("where_i_live", 0.403, source_class="asserted"),
        _hit("hint:9350f4cc", 0.66, source_class="asserted"),
        _hit(
            "procedure_retrieve_user_location",
            0.56,
            source_class="asserted",
            memory_type="procedural",
        ),
        _hit("summary:abc", 0.47, source_class="asserted"),
        _hit("chunk:abc:0", 0.48, source="chunk", source_class="asserted"),
    ]
    assert asserted_component(hits) == 1
    boosts = _effective_arm_boosts(hits, component="asserted", rrf_pool=False, scale=0.03)
    assert boosts == {1: 0.03}
    fused = [h.rank_score + boosts.get(i, 0.0) for i, h in enumerate(hits)]
    assert fused[1] > fused[0], "the asserted key now outranks the extracted preference row"
    assert fused[2] > fused[1], "a stronger match is never overtaken (0184)"


def test_boost_is_capped_by_the_pool_and_off_at_zero_scale() -> None:
    hits = [_hit("k", 0.01, source_class="asserted")]
    asserted_component(hits)
    assert _effective_arm_boosts(hits, component="asserted", rrf_pool=False, scale=0.03) == {
        0: 0.01
    }
    src = inspect.getsource(
        __import__("server.memory.app.search.ranking", fromlist=["x"])._apply_global_fusion
    )
    assert "asserted_component(all_hits)" in src and "_asserted_scale > 0" in src
    assert "fused = raw + boost + kboost + aboost" in src


def test_rrf_pool_joins_the_arm_by_rank() -> None:
    hits = [_hit("a", 0.5, source_class="asserted"), _hit("b", 0.4, source_class="extracted")]
    asserted_component(hits)
    assert _effective_arm_boosts(hits, component="asserted", rrf_pool=True, scale=0.03) == {
        0: 1.0 / 61.0
    }
