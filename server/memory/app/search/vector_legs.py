"""The main vector legs: the per-namespace cosine pool (private-first and
token-order tiers for cross-namespace scope), scored into ctx.all_hits, and the
wire-hit fields every leg copies."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from server.core.vectors.private_first import best_similarity, each, search_private_first
from server.identity.app import namespace_visibility
from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.context import SearchContext
from server.memory.app.search.filters import read_filters
from server.memory.app.search.graph import _graph_via_path
from server.memory.app.search.knobs import (
    _recency_pool_weight,
    _reinforcement_half_life,
    _reinforcement_half_life_use_scale,
    _reinforcement_weight,
    _resolve_prior_cap,
    _sensitivity_penalty,
    _source_class_boost_eligible,
)
from server.memory.app.search_tag_filter import stamp_row_tags as _stamp_row_tags
from server.memory.app.search_tag_filter import tagged_row_hit as _tagged_row_hit
from server.memory.domain.search_types import hit_sort_key
from server.observability import degradations as _degradations

log = logging.getLogger(__package__)


def _plane_fields(h: Any) -> dict[str, Any]:
    """The retrieval-plane fields every wire hit copies from an internal hit: `source` ("fact" |
    "chunk" | …) and, for a chunk hit, the parent fact's key (`memory_chunks.parent_memory_key`)
    so a consumer folds chunk hits onto their parent without parsing the `chunk:<session>:<n>`
    id."""
    return {
        "source": str(getattr(h, "source", "fact") or "fact"),
        "parent_memory_key": str(getattr(h, "parent_memory_key", "") or ""),
    }


def _search_query_for(ctx: SearchContext, ns: str) -> Any:
    from server.memory.app.retrieval import SearchQuery

    input = ctx.input
    return SearchQuery(
        org=input.org,
        namespace=ns,
        **read_filters(input),
        embedding_model=ctx.embedder.model_name,
        query_vec=ctx.query_vec,
        top_k=ctx.pool_top_k,
        key_axis=getattr(input, "key_axis", None),
        entity_filter=ctx.effective_entity_filter,
        query_text=input.query,
        search_mode=input.search_mode,
        alpha=input.alpha,
        include_top_n_unmatched=input.include_top_n_unmatched,
        execution_planes=list(ctx.search_plan.planes),
        graph_expanded=ctx.graph_expanded_by_ns.get(ns, []),
        triple_matched_keys=ctx.triple_matched_by_ns.get(ns, []),
        query_anchor_iso=ctx.query_anchor_iso,
        preference_attribute=ctx.preference_attribute,
        recency_intent=ctx.recency_intent,
        chronological_intent=ctx.chronological_intent,
        tag_boost=float(getattr(input, "tag_boost", 0.3)),
        category_boost=float(getattr(input, "category_boost", 0.3)),
        confidence_weight=float(getattr(input, "confidence_weight", 0.1)),
        prior_cap=_resolve_prior_cap(input),
        personalization=str(getattr(input, "personalization", "full") or "full"),
        source_class_boost_eligible=_source_class_boost_eligible(),
        demote_tags=list(getattr(input, "demote_tags", []) or []),
        sensitivity_penalty=_sensitivity_penalty(input),
        rrf_adaptive_lexical=getattr(input, "rrf_adaptive_lexical", None),
        reinforcement_weight=_reinforcement_weight(input),
        reinforcement_half_life_days=_reinforcement_half_life(input),
        reinforcement_half_life_use_scale=_reinforcement_half_life_use_scale(input),
        recency_pool_weight=_recency_pool_weight(input),
        chunk_max_share=getattr(input, "chunk_max_share", None),
    )


def _leg_wanted(input: Any, leg: str) -> bool:
    """`legs` names the legs the caller can show; None (every caller that never heard of the
    field) means every leg. A caller that cannot show a session digest (the Memories page)
    leaves the session-summary leg out, which saves most of that request's read (~24 of 63 MB
    and 800 ms in one measured keystroke)."""
    legs = getattr(input, "legs", None)
    return legs is None or leg in legs


async def _no_legs() -> list[Any]:
    """The answer of a leg the caller left out."""
    return []


def _leg_or_empty(input: Any, leg: str, run: Any) -> Any:
    """`run()`'s coroutine when the caller wants `leg`, else an empty answer — so a left-out
    leg costs no statement."""
    return run() if _leg_wanted(input, leg) else _no_legs()


async def _search_one(ctx: SearchContext, ns: str) -> tuple[str, list[Any]]:
    from server.memory.app.retrieval import _is_empty_block_column_error, vector_search

    ctx.actual_work["vector_queries"] = int(ctx.actual_work["vector_queries"]) + 1
    try:
        return ns, await vector_search(_search_query_for(ctx, ns))
    except Exception as e:
        if not _is_empty_block_column_error(e):
            raise
        # ClickHouse code 10 on a part written seconds earlier survived the
        # leg's own retries. A 500 here fails the whole ask right after a
        # write — the moment an agent most often reads back. Fail open: an
        # empty leg for this namespace, recorded on `degraded` so the caller
        # can see the answer was drawn from less than the store holds.
        log.warning("memory.search vector leg failed open on fresh-part error (ns=%s): %s", ns, e)
        _degradations.note("clickhouse", "vector_leg_empty", "fresh-part code 10 after retries")
        return ns, []


async def _run_vector_legs(
    ctx: SearchContext,
    entity_boost_task: asyncio.Task[dict[tuple[str, str], float]] | None,
) -> None:
    """Run the main per-namespace vector legs (with private-first sequential
    fallback for cross-namespace scope), join the entity-ANN boost map, and
    build the initial scored pool into ctx.all_hits.

    `entity_boost_task` was launched before this stage so its CH read overlaps
    the vector legs; it is joined after the gather, right before the pool is scored."""
    from server.observability.otel import traced

    input = ctx.input

    # Private-first sequential fallback (cross-namespace only, unscoped /
    # order-less tokens): search the addressed namespace and the private ones
    # first; if a hit there clears the relevance floor, skip the public pass —
    # otherwise also search the public ones and merge. Without this a
    # strong private hit gets diluted by team-scratchpad noise.
    # CAVEAT — visibility is a rough OWNERSHIP proxy here, nothing more. It is
    # orthogonal to the caller's role: a private namespace the caller only
    # *reads* (someone else's, shared to them) still outranks a public
    # namespace the caller *writes* to. This is a best-effort default only for
    # tokens that supply no explicit order; scoped tokens (ns_roles) bypass it
    # entirely and honor their explicit priority list (the branch below).
    # Threshold tuned for real embedders (genuine matches 0.4+, noise
    # 0.10–0.25). The dev fake-ngram embedder is noisier, so we tune for
    # production and accept dev may fall through to public unnecessarily —
    # a missing public hit is worse than a wasted public query.
    from server.settings import settings as _tier_settings

    PRIVATE_HIT_THRESHOLD = float(_tier_settings.memory_search_private_first_stop_similarity)

    # Strict token-order waterfall (scoped tokens carrying `ns_roles`): search
    # the addressed namespace, then the others one tier at a time in the
    # token's priority order, and stop at the first tier that clears the
    # relevance floor. No visibility- or ownership-based reordering
    # (public-first if that's what the token says).
    if (
        ctx.namespace_order_authoritative
        and input.namespace_scope == "all_mine"
        and len(ctx.namespaces) > 1
    ):
        # Separate from PRIVATE_HIT_THRESHOLD: this is the stop
        # cutoff for the priority-ordered tier waterfall and is tunable per
        # embedder. Default 0.40 sits at the genuine-match floor, so only a
        # confident higher-priority hit suppresses the lower-priority search.
        TIER_HIT_THRESHOLD = float(_tier_settings.memory_search_tier_stop_similarity)
        results_per_ns: list[Any] = []
        for _tier_idx, ns in enumerate(ctx.namespaces):
            with traced(
                "ch.query.memory_embeddings",
                op="vector_search.tier",
                top_k=input.top_k,
                namespaces=1,
            ):
                tier = await asyncio.gather(_search_one(ctx, ns))
            results_per_ns = list(results_per_ns) + list(tier)
            if best_similarity(tier) >= TIER_HIT_THRESHOLD:
                break
    else:
        # ---- unscoped path (no scoped-token order): visibility-based private-first
        # fallback, else a flat concurrent fan-out. Visibility is TTL-cached, so a
        # multi-namespace search does not pay a `loom.namespaces FINAL` round trip.
        fan_out = input.namespace_scope == "all_mine"
        results_per_ns = await search_private_first(
            input.namespace,
            list(ctx.namespaces),
            each(lambda ns: _search_one(ctx, ns)),
            visibility=(
                functools.partial(namespace_visibility.visibility_map, input.org)
                if fan_out
                else None
            ),
            threshold=PRIVATE_HIT_THRESHOLD,
            span=functools.partial(traced, "ch.query.memory_embeddings", top_k=input.top_k),
        )

    # Join the entity-store ANN task launched before the vector legs.
    if entity_boost_task is not None:
        try:
            ctx.entity_boost_by_key = await entity_boost_task
        except Exception as e:  # noqa: BLE001 — entity boost is optional; search runs without it
            log.warning("entity_vectors ANN read failed (non-fatal): %s", e)

    # Cross-namespace ordering is pure score-rank: private-first is already
    # enforced upstream (the private pass runs alone when it has a strong
    # hit), so when public rows are present we WANT a strong public hit to
    # outrank noisy private rows, which score-rank does.

    # Each hit carries its RAW per-arm rank_score in
    # score_components["rank_score_raw"]; the ANN boost is recorded but not
    # yet folded in. The whole pool (main + sub-query + anchor arms) is fused
    # in ONE global pass below so the boost composition can enforce its scale
    # invariant against the complete pool. See `_apply_global_fusion`.
    scored: list[tuple[float, _MemorySearchHit]] = []
    _include_tags = bool(getattr(ctx.input, "include_tags", False))
    for ns, hits in results_per_ns:
        for h in hits:
            raw = float(h.rank_score)
            ann_boost = ctx.entity_boost_by_key.get((ns, h.memory_key), 0.0)
            score_components = dict(getattr(h, "score_components", {}) or {})
            score_components["rank_score_raw"] = raw
            score_components["entity_ann_boost"] = float(ann_boost)
            scored.append(
                (
                    raw,
                    _wire_hit(
                        ctx,
                        ns,
                        h,
                        rank_score=raw,
                        score_components=score_components,
                        include_tags=_include_tags,
                    ),
                )
            )
    scored.sort(key=lambda x: (-x[0], hit_sort_key(x[1])[1:]))
    ctx.all_hits = [h for _, h in scored]


def _wire_hit(
    ctx: SearchContext,
    ns: str,
    h: Any,
    *,
    rank_score: float,
    score_components: dict[str, Any],
    include_tags: bool,
) -> Any:
    """The wire hit for a vector-leg service hit (main or supplemental): every field the
    later stages read (`created_at` for the fresh-hit reread, `derived_from`, the provenance
    pair), the row's tags kept privately for the belt, and on the wire only when asked."""
    tags = getattr(h, "tags", None)
    return _stamp_leg_tags(
        h,
        _MemorySearchHit(
            memory_key=h.memory_key,
            memory_type=h.memory_type,
            content_excerpt=h.content_excerpt,
            distance=h.distance,
            similarity=1.0 - h.distance,
            rank_score=rank_score,
            via_path=_graph_via_path(ctx, ns, h),
            source_class=str(getattr(h, "source_class", "") or ""),
            temporal_anchor=getattr(h, "temporal_anchor", "") or "",
            temporal_confidence=float(getattr(h, "temporal_confidence", 0.0) or 0.0),
            valid_at=getattr(h, "valid_at", "") or "",
            created_at=str(getattr(h, "created_at", "") or ""),
            **_plane_fields(h),
            status=str(getattr(h, "status", "") or ""),
            version=int(getattr(h, "version", 0) or 0),
            source_resource_id=getattr(h, "source_resource_id", "") or "",
            verified=bool(getattr(h, "verified", False)),
            session_id=getattr(h, "session_id", "") or "",
            speaker=getattr(h, "speaker", "") or "",
            about_other=bool(getattr(h, "about_other", False)),
            derived_from=list(getattr(h, "derived_from", []) or []),
            # The dense leg's `e.tags` ride along when asked; None keeps the key off the wire.
            tags=[str(x) for x in tags if x] if include_tags and tags else None,
            namespace=ns,
            score_components=score_components,
        ),
    )


def _stamp_leg_tags[HitT](leg_hit: Any, wire_hit: HitT) -> HitT:
    """A service-layer hit from a fact-table leg (fact, lexical union, unmatched, query
    neighbour) read the row's `tags`; keep them on the wire hit privately. Chunk / summary /
    record hits read none and stay unstamped (the belt does not judge them). The row's
    stored identity, or a folded set chunk's writer, rides along the same way."""
    if _tagged_row_hit(wire_hit):
        _stamp_row_tags(wire_hit, getattr(leg_hit, "tags", None))
    if (identity := getattr(leg_hit, "identity", None)) is not None:
        wire_hit._row_identity = identity  # type: ignore[attr-defined]
    if (writer := getattr(leg_hit, "writer", None)) is not None:
        wire_hit._row_writer = writer  # type: ignore[attr-defined]
    return wire_hit
