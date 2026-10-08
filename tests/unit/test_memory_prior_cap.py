"""Capped discretionary priors: the memory plane's port of the code plane's
'signal can never outrank relevance' rule. Pins the cap contract on the pure
SQL-fragment builder so it needs no DB."""

from __future__ import annotations

from server.memory.app.retrieval.fact_rank import _prior_group_expr


def test_uncapped_is_legacy_additive_group() -> None:
    expr, capped = _prior_group_expr("0.3 * frac", "0.3", 0.0)
    assert not capped
    assert "least(" not in expr
    assert "{confidence_weight:Float32} * e.confidence" in expr
    assert "0.3 * frac" in expr and "0.3" in expr


def test_positive_cap_wraps_whole_group_in_least() -> None:
    expr, capped = _prior_group_expr("T", "C", 0.05)
    assert capped
    assert expr.startswith("least({prior_cap:Float32}, ")
    # confidence + both boosts are all inside the single cap, not each capped
    assert expr.count("least(") == 1
    assert "e.confidence" in expr and "(T)" in expr and "(C)" in expr


def test_negative_or_zero_cap_never_wraps() -> None:
    for c in (0.0, -1.0):
        _, capped = _prior_group_expr("T", "C", c)
        assert not capped


def test_conflicted_penalty_is_gated_off_by_default() -> None:
    """The conflicted-demotion term only appears when conflicted_penalty>0;
    default 0.0 keeps ranking byte-identical (no measured gain, so it is an
    off-by-default operator lever)."""
    from server.memory.domain.search_types import SearchQuery

    q = SearchQuery(
        org="o",
        namespace="n",
        scope="private",
        agent="-",
        embedding_model="m",
        query_vec=[0.1],
        user_id="-",
        query_text="x",
        search_mode="semantic",
        top_k=5,
    )
    assert q.conflicted_penalty == 0.0
