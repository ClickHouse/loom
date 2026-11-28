"""Calibration and abstention on the legs' own rows, before any prior reorders them."""

from __future__ import annotations

from server.code.app.search.state import _SearchState
from server.code.app.search.voting import _apply_voting_leg_outcomes
from server.code.domain.index import search_confidence
from server.memory.domain.retrieval_calibration import RetrievalCalibration, finalize_abstain
from server.settings import settings


def _calibrate(
    st: _SearchState,
    *,
    abstain_threshold: float,
    confident_similarity: float,
) -> bool:
    """Calibrate the result and decide abstention on what the two content legs
    returned, before any prior reorders it; returns whether they found the same unit.

    The lexical rows read here are the leg's own first rows (`st.lexical_head`), not
    the pool it cut on the path penalties: the penalties are priors, and no prior
    decides abstention."""
    result, constrained = st.result, st.constrained
    vector_rows = st.vector_rows
    agreement = _leg_agreement(st)
    if vector_rows:
        top_sim = 1.0 - float(vector_rows[0]["distance"])
        similarity = search_confidence.similarity_term(
            top_sim,
            abstain_threshold=abstain_threshold,
            confident_similarity=confident_similarity,
        )
        confidence = search_confidence.confidence(similarity, agreement)
        explanation = (
            f"top_similarity={top_sim:.2f}, similarity={similarity:.2f}, "
            f"leg_agreement={agreement:.2f}"
        )
    else:
        # No cosine to read: the request skipped the vector leg, no embedder is
        # configured or the leg failed. The stand-in sits at the threshold, so the
        # result neither abstains on a similarity it does not have nor reads low risk.
        top_sim = abstain_threshold
        confidence = search_confidence.NO_COSINE_CONFIDENCE
        explanation = "no vector leg, top_similarity unknown"
    # only a real cosine is reported; the stand-in is not one
    result.top_similarity = round(top_sim, 4) if vector_rows else 0.0

    risk = search_confidence.risk_band(
        confidence,
        abstain_below=float(settings.code_search_abstain_confidence_below),
        medium_from=float(settings.code_search_medium_risk_from),
        low_from=float(settings.code_search_low_risk_from),
    )
    calibration = RetrievalCalibration(
        confidence=round(confidence, 3),
        risk="abstain" if top_sim < abstain_threshold else risk,
        route_agreement=agreement,
        explanation=explanation + (", constrained=true" if constrained else ""),
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
    _apply_voting_leg_outcomes(result, st.card_stats, st.concept_stats, card_gate=st.card_gate)
    if result.abstain and result.abstain_reason and result.abstain_code != "off_domain":
        result.abstain_reason += (
            " (every unit here does contain the pattern; the low similarity is about "
            "the question, not the match)"
            if constrained
            else " (grep the working tree instead of trusting these)"
        )

    return agreement == 1.0


def _leg_agreement(st: _SearchState) -> float:
    """How far the lexical leg confirms the vector leg's first unit. A leg that did
    not answer (skipped by the request, failed, dropped by the deadline, no embedder)
    is neither a confirmation nor its absence; a lexical leg that answered with no row
    confirms nothing."""
    answered = (
        st.run_lexical
        and bool(st.vector_rows)
        and not st.work.get("lexical_leg_failed")
        and "lexical" not in st.result.legs_timed_out
    )
    if not answered:
        return search_confidence.AGREEMENT_UNKNOWN
    return search_confidence.leg_agreement(st.vector_rows, st.lexical_head)
