"""Provenance as a ranking term and probation for unattested external rows.
CH-free."""

from __future__ import annotations

from types import SimpleNamespace as NS

from server.memory.domain import probation


def test_trust_term_is_the_literal_zero_when_off() -> None:
    params: dict = {}
    assert probation.trust_boost_sql(0.0, params) == "0.0"
    assert probation.trust_boost_sql(-1.0, params) == "0.0"
    assert params == {}


def test_trust_term_binds_the_weight_and_reads_class_and_attestation() -> None:
    params: dict = {}
    expr = probation.trust_boost_sql(0.1, params)
    assert params == {"trust_w": 0.1}
    assert "{trust_w:Float32}" in expr and "toFloat32(e.verified)" in expr
    for cls, w in (
        ("external", "-1.0"),
        ("imported", "-0.25"),
        ("asserted", "0.5"),
        ("observed", "0.5"),
    ):
        assert cls in expr and w in expr
    # extracted / derived / unknown fall to the multiIf default of 0.0
    assert expr.rstrip(")").endswith("0.0) + toFloat32(e.verified)") or "0.0)" in expr


def test_probation_holds_only_unattested_external_rows() -> None:
    hits = [
        NS(memory_key="a", source_class="external", verified=False),
        NS(memory_key="b", source_class="external", verified=True),
        NS(memory_key="c", source_class="asserted", verified=False),
        NS(memory_key="d", source_class="", verified=False),
        NS(memory_key="e"),  # a hit model without the fields → never held
    ]
    kept, held = probation.exclude_unverified_external(hits)
    assert [h.memory_key for h in kept] == ["b", "c", "d", "e"] and held == 1
    assert probation.is_probationary(hits[0]) and not probation.is_probationary(hits[1])


def test_rank_expression_carries_the_term_in_the_correctness_tail() -> None:
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2]
        / "server"
        / "memory"
        / "app"
        / "retrieval"
        / "fact_rank.py"
    ).read_text()
    # both tails (bare and default) append it next to the conflicted demotion,
    # and no per-mode relevance expression carries it
    assert src.count("({t.trust_boost_expr})") == 2
    assert "+ {t.trust_boost_expr}" not in src.replace("({t.trust_boost_expr})", "")
