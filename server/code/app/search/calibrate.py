"""Calibration and abstention on the raw fusion, before any prior reorders it."""

from __future__ import annotations

from typing import Any

from server.code.app.search.state import _SearchState
from server.code.app.search.voting import _apply_voting_leg_outcomes
from server.memory.domain.retrieval_calibration import calibrate_retrieval, finalize_abstain
from server.settings import settings


def _calibrate(
    st: _SearchState,
    fused: list[tuple[dict[str, Any], float]],
    *,
    run_vector: bool,
    noise_floor: float,
    abstain_threshold: float,
) -> bool:
    """Calibrate the fused list and decide abstention on it, before any
    prior reorders it; returns whether the two content legs agreed."""
    result, constrained, fuzzy_hit = st.result, st.constrained, st.fuzzy_hit
    lexical_rows, vector_rows = st.lexical_rows, st.vector_rows
    card_gate, card_stats, concept_stats = st.card_gate, st.card_stats, st.concept_stats
    # similarity signal for calibration: cosine of the top vector hit when the
    # vector leg ran; else the lexical top presence stands in at the floor.
    # A vector leg the REQUEST skipped (legs="lexical") cannot count against
    # the result: the stand-in then sits at the threshold, not below it.
    if vector_rows:
        top_sim = _top_similarity(fused, vector_rows)
    elif not run_vector:
        top_sim = max(noise_floor + 0.1, abstain_threshold)
    else:
        top_sim = noise_floor + 0.1
    second_sim = _second_similarity(fused, vector_rows)
    # only a real cosine is reported; the lexical stand-in is not one
    result.top_similarity = round(top_sim, 4) if vector_rows else 0.0

    lex_ids = {str(r["chunk_id"]) for r in lexical_rows[:10]}
    vec_ids = {str(r["chunk_id"]) for r in vector_rows[:10]}
    both_legs = bool(lexical_rows) and bool(vector_rows)
    legs_agree = both_legs and bool(lex_ids & vec_ids)

    calibration = calibrate_retrieval(
        top_similarity=top_sim,
        second_similarity=second_sim,
        result_count=len(fused),
        abstain_threshold=abstain_threshold,
        confidence_abstain_below=float(settings.code_search_abstain_confidence_below),
        medium_risk_from=settings.code_search_medium_risk_from,
        low_risk_from=settings.code_search_low_risk_from,
        **calibration_routes(legs_agree=legs_agree, both_legs=both_legs, fuzzy_hit=fuzzy_hit),
        constrained=constrained,
    )
    result.retrieval_confidence = calibration.confidence
    result.route_agreement = calibration.route_agreement
    result.calibration_explanation = calibration.explanation
    result.retrieval_risk = calibration.risk

    result.abstain, result.abstain_code, result.abstain_reason = finalize_abstain(
        top_similarity=top_sim,
        calibration=calibration,
        abstain_threshold=abstain_threshold,
        no_match_reason=(
            "no strong match in the code index — use grep on the working tree instead"
        ),
    )
    _apply_voting_leg_outcomes(result, card_stats, concept_stats, card_gate=card_gate)
    if result.abstain and result.abstain_reason and result.abstain_code != "off_domain":
        result.abstain_reason += (
            " (every unit here does contain the pattern; the low similarity is about "
            "the question, not the match)"
            if constrained
            else " (grep the working tree instead of trusting these)"
        )

    return legs_agree


def calibration_routes(*, legs_agree: bool, both_legs: bool, fuzzy_hit: bool) -> dict[str, int]:
    """The extra-route votes the code plane hands calibrate_retrieval, made legs-aware:
    leg agreement is a route only when BOTH legs ran and returned rows. A single-leg run
    (legs="lexical"/"vector", no embedder, a failed leg) therefore has one extra route —
    the fuzzy-symbol vote — and is not scored as "the legs disagreed"; the fuzzy vote
    counts the same in both cases."""
    possible = (1 if both_legs else 0) + 1
    votes = int(bool(legs_agree) and both_legs) + int(fuzzy_hit)
    return {"extra_route_votes": votes, "extra_routes_possible": possible}


def _top_similarity(
    fused: list[tuple[dict[str, Any], float]], vector_rows: list[dict[str, Any]]
) -> float:
    dist_by_id = {str(r["chunk_id"]): float(r["distance"]) for r in vector_rows}
    for row, _score in fused:
        d = dist_by_id.get(str(row["chunk_id"]))
        if d is not None:
            return 1.0 - d
    return 0.0


def _second_similarity(
    fused: list[tuple[dict[str, Any], float]], vector_rows: list[dict[str, Any]]
) -> float:
    dist_by_id = {str(r["chunk_id"]): float(r["distance"]) for r in vector_rows}
    seen = 0
    for row, _score in fused:
        d = dist_by_id.get(str(row["chunk_id"]))
        if d is not None:
            seen += 1
            if seen == 2:
                return 1.0 - d
    return 0.0
