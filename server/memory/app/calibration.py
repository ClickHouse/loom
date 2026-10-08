"""Calibration inspection handler for `calibration.get`."""

from __future__ import annotations

from server.memory.api.models_calibration import CalibrationGetInput, CalibrationGetOutput
from server.settings import settings


async def handle_calibration_get(input: CalibrationGetInput) -> CalibrationGetOutput:
    """Resolve the active calibration for (org, namespace): per-namespace,
    then cluster-wide, then scalar fallback, with the source tier reported."""
    from server.memory.app.embedder_calibration import (
        get_calibration,
        get_namespace_calibration,
    )

    ns_cal = await get_namespace_calibration(input.org, input.namespace)
    cluster_cal = get_calibration()

    # Per-namespace wins over cluster-wide; both render the same field set.
    cal = ns_cal or cluster_cal
    if cal is not None:
        return CalibrationGetOutput(
            source="namespace" if ns_cal is not None else "cluster",
            embedder_model=cal.embedder_model,
            noise_floor=cal.noise_floor,
            rerank_skip_above=cal.rerank_skip_above,
            hyde_fallback_threshold=cal.hyde_fallback_threshold,
            query_rewrite_threshold=cal.query_rewrite_threshold,
            abstain_threshold=cal.abstain_threshold,
            conflict_threshold=cal.conflict_threshold,
            duplicate_threshold=cal.duplicate_threshold,
            similar_p10=cal.similar_p10,
            similar_p25=cal.similar_p25,
            similar_p50=cal.similar_p50,
            similar_p75=cal.similar_p75,
            distractor_p50=cal.distractor_p50,
            distractor_p90=cal.distractor_p90,
            similar_pairs_n=cal.similar_pairs_n,
            distractor_pairs_n=cal.distractor_pairs_n,
        )

    # Scalar fallback: neither probe produced a result (fake embedder, or
    # boot probe failed).
    return CalibrationGetOutput(
        source="scalar",
        embedder_model=settings.embedding_model,
        noise_floor=settings.memory_search_min_similarity,
        rerank_skip_above=settings.llm_rerank_skip_above,
        hyde_fallback_threshold=settings.memory_search_hyde_fallback_threshold,
        query_rewrite_threshold=settings.memory_search_query_rewrite_threshold,
        abstain_threshold=settings.memory_search_abstain_threshold,
        conflict_threshold=settings.conflict_threshold,
        duplicate_threshold=settings.duplicate_threshold,
    )
