"""The response's verdict: calibrated confidence, the abstain decision, and the
per-row below-threshold flag."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.context import SearchContext
from server.memory.app.search.scope import _SearchRun
from server.memory.domain.search_types import TIER_COMPONENTS

if TYPE_CHECKING:
    from server.memory.domain.retrieval_calibration import RetrievalCalibration


def gate_order(hits: list[Any]) -> list[Any]:
    """The served rows as the confidence gates read them: the rows an external reranker moved
    back at their fused positions (`rerank_from`); rows under the similarity floor stay put."""
    comps = [h.score_components for h in hits]
    slots = [i for i, c in enumerate(comps) if "rerank_from" in c and not c.get("below_floor")]
    if not any(comps[i].get("rerank_delta") for i in slots):
        return hits
    fused = [hits[i] for i in sorted(slots, key=lambda i: float(comps[i]["rerank_from"]))]
    out = list(hits)
    for i, h in zip(slots, fused, strict=True):
        out[i] = h
    return out


def _gate_top_sim(gated: list[Any]) -> float | None:
    """The cosine the below-threshold gate reads, over rows in the gates' order
    (`gate_order`): the first row's by default, or the best cosine among them when
    memory_search_abstain_top_sim_scope == "window" — under rrf a lexical-only match can
    hold rank 1 while the relevant row sits at rank 2 with 0.36."""
    if not gated:
        return None
    from server.settings import settings as _s_gate

    if str(_s_gate.memory_search_abstain_top_sim_scope) == "window":
        return max(1.0 - float(h.distance) for h in gated)
    return 1.0 - float(gated[0].distance)


# Hit `source` values whose `distance` is a genuine cosine — the planes the
# per-hit flag may judge. Mirrors `_COSINE_PLANES` in observability/metrics.
_FLAG_COSINE_PLANES: frozenset[str] = frozenset(
    {"fact", "chunk", "summary", "entity_summary", "observation", "anchor"}
)


def _flag_below_threshold(hits: list[Any], abstain_threshold: float) -> int:
    """Mark every served row whose un-boosted similarity is below the abstain threshold the
    response's gate used. The gate reads the first row of `gate_order`; a k=30 window carries
    a tail the gate never looked at, and a caller who wants "abstain means abstain" per row
    reads this flag.
    Rows without a cosine (tier rows, derived / computed evidence, record-plane lexical rows)
    are left unset. Returns how many rows were flagged."""
    if abstain_threshold <= 0.0 or not hits:
        return 0
    flagged = 0
    for h in hits:
        if str(getattr(h, "source", "fact") or "fact") not in _FLAG_COSINE_PLANES:
            continue
        comps = getattr(h, "score_components", None) or {}
        if any(c in comps for c in TIER_COMPONENTS):
            continue
        try:
            sim = 1.0 - float(getattr(h, "distance", 1.0))
        except (TypeError, ValueError):
            continue
        if sim < abstain_threshold:
            try:
                h.below_threshold = True
                flagged += 1
            except Exception:  # noqa: BLE001 — a frozen test double stays as is
                continue
    return flagged


def _finalize_abstain(
    hits: list[Any],
    *,
    calibration: Any,
    abstain_threshold: float,
    query_text: str = "",
    work: dict[str, Any] | None = None,
) -> tuple[bool, str, str]:
    """Decide the abstain envelope for a finished hit list; `hits` arrive in the gates'
    order (`gate_order`).

    Precedence is load-bearing and matches the main path: an empty set is `no_match`, a weak
    top hit is `below_threshold` (carrying the actual threshold, which is what an operator
    tuning LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD needs to see), and only then does the
    calibrator's verdict apply. Reporting `low_calibrated_confidence` for every abstain would
    hide `below_threshold` in `loom.search.abstain{reason}` and tell the user "confidence is too
    low" when the truth is "nothing survived retrieval".

    Returns (abstain, abstain_code, abstain_reason). Also marks every served row whose own
    similarity is below `abstain_threshold` with `below_threshold` and counts them into `work`
    when given — the row-level companion of the response-level verdict.

    The identifier-grounding leg (LOOM_MEMORY_SEARCH_IDENTIFIER_GROUNDING, default on): a query
    that names a salient identifier absent from every hit abstains even above the cosine
    threshold (see the setting's comment); with the setting off the reason stays None.
    """
    from server.memory.domain.retrieval_calibration import (
        finalize_abstain,
        identifier_grounding_applies,
        identifier_grounding_missing,
    )
    from server.settings import settings as _s

    grounding_reason: str | None = None
    if (
        _s.memory_search_identifier_grounding
        and query_text
        and hits
        and identifier_grounding_applies(
            query_text,
            int(_s.memory_search_identifier_grounding_max_tokens),
        )
    ):
        # The key is part of the row (`finding_h_pr116016` names its PR); grounding reads it
        # beside the excerpt, as the identifier pin already does.
        grounding_reason = identifier_grounding_missing(
            query_text,
            [
                f"{getattr(h, 'memory_key', '') or ''} {getattr(h, 'content_excerpt', '') or ''}"
                for h in hits
            ],
        )

    flagged = _flag_below_threshold(hits, abstain_threshold)
    if flagged and work is not None:
        work["below_threshold_hits"] = flagged
    return finalize_abstain(
        top_similarity=_gate_top_sim(hits),
        calibration=calibration,
        abstain_threshold=abstain_threshold,
        no_match_reason="no memories matched the query above the noise floor",
        grounding_reason=grounding_reason,
    )


def _calibrate_final_hits(
    ctx: SearchContext,
    run: _SearchRun,
    final_hits: list[_MemorySearchHit],
    evidence_support_count: int,
) -> tuple[RetrievalCalibration, tuple[bool, str, str]]:
    """Phase 21: the verdict. The calibrated confidence and the abstain
    decision. Returns the calibration and the (abstain, abstain_code,
    abstain_reason) triple."""
    from server.memory.app.embedder_calibration import effective_abstain_threshold
    from server.settings import settings as _settings

    input = ctx.input
    actual_work = ctx.actual_work
    # Abstention signal: tell the agent to say "I don't know" instead of
    # hallucinating from noise. Two triggers:
    #   (1) no hits survived the noise floor (clean abstention)
    #   (2) top hit similarity below abstain_threshold (weak abstention)
    abstain_threshold = effective_abstain_threshold(
        _settings.memory_search_abstain_threshold,
        run.ns_calibration,
    )
    from server.memory.domain.retrieval_calibration import calibrate_retrieval

    # KNOWN ISSUE, deliberately not fixed here. The first row of `gate_order` (the served rank
    # 1, or after a rerank the fused rank 1) is not guaranteed to carry the best
    # cosine: RRF/lexical fusion can put a weak row first, and under the default
    # namespace_scope="all_mine" a row from another namespace can win it, so a query can
    # abstain while a 0.63 hit sits at rank 1. Taking max/second-max by value instead is wrong
    # three ways: (a) `distance` is not a cosine on every hit — record-plane rows store 1.0 -
    # lexical_rank_score (a token-overlap score), so a max reads that as a similarity and
    # suppresses abstention indefinitely; (b) second-highest-by-value is by construction the
    # smallest gap in the set, which collapses `calibrate_retrieval`'s margin term and invents
    # a *new* class of false abstention ([0.35, 0.05, 0.34] goes 0.443/"high" ->
    # 0.327/"abstain"); (c) the iterative judge reads the same first row, and would disagree
    # with the verdict about what the top hit is.
    # A real fix needs the hit model to distinguish cosine from synthesized distances.
    # One list for every gate below: the served rows in fused order (a reranker's moves
    # undone among them).
    gated = gate_order(final_hits)
    second_similarity = 1.0 - gated[1].distance if len(gated) > 1 else 0.0
    calibration = calibrate_retrieval(
        top_similarity=_gate_top_sim(gated) or 0.0,
        second_similarity=second_similarity,
        result_count=len(final_hits),
        graph_seeds=run.graph_seeds,
        graph_neighbors=run.graph_neighbors,
        triples_extracted=len(ctx.query_triples),
        triples_matched=run.triples_matched,
        evidence_support_count=evidence_support_count,
        abstain_threshold=abstain_threshold,
    )
    # The first row of `gate_order`, as the calibrator above; see the note there before
    # changing it.
    abstain, abstain_code, abstain_reason = _finalize_abstain(
        gated,
        calibration=calibration,
        abstain_threshold=abstain_threshold,
        query_text=input.query,
        work=actual_work,
    )
    return calibration, (abstain, abstain_code, abstain_reason)
