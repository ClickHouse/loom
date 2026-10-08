"""Supplemental vector legs over the main search's filters: query decomposition
sub-queries and set-question expansion phrases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.context import SearchContext
from server.memory.app.search.vector_legs import _search_query_for, _wire_hit
from server.memory.domain import set_expansion as _set_expansion

log = logging.getLogger(__package__)


async def _supplemental_leg(
    ctx: SearchContext,
    sub_q: str,
    top_k: int,
    seen_keys: set[tuple[str, str, str]],
) -> list[_MemorySearchHit]:
    """One supplemental vector leg over every namespace in scope: embed `sub_q`, run it with the
    main search's filters and posture, return the hits not already in `seen_keys` (which it
    extends). Shared by query decomposition (sub-queries) and set-question expansion; hits
    carry their RAW rank_score into the shared pool."""
    from server.memory.app.retrieval import vector_search

    input = ctx.input
    try:
        ctx.actual_work["embedding_calls"] = int(ctx.actual_work["embedding_calls"]) + 1
        sub_vec = await ctx.embedder.embed(sub_q)
    except Exception:  # noqa: BLE001 — a failed sub-query embed drops only that sub-query
        return []
    ctx.actual_work["vector_queries"] = int(ctx.actual_work["vector_queries"]) + len(ctx.namespaces)
    sub_per_ns = await asyncio.gather(
        *[
            vector_search(
                replace(
                    _search_query_for(ctx, ns), query_vec=sub_vec, top_k=top_k, query_text=sub_q
                )
            )
            for ns in ctx.namespaces
        ]
    )
    include_tags = bool(getattr(input, "include_tags", False))
    out: list[_MemorySearchHit] = []
    for ns_name, hits in zip(ctx.namespaces, sub_per_ns, strict=True):
        for h in hits:
            key = (ns_name, h.memory_key, h.content_excerpt)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(
                _wire_hit(
                    ctx,
                    ns_name,
                    h,
                    rank_score=float(h.rank_score),
                    score_components=dict(getattr(h, "score_components", {}) or {}),
                    include_tags=include_tags,
                )
            )
    # No per-sub-query rescaling: hits carry their RAW rank_score into the shared pool and are
    # fused with every other arm in the single global fusion pass below.
    return out


async def _run_set_expansion(ctx: SearchContext) -> None:
    """Set-question instance legs. Each phrase the expansion wrote runs as a narrow
    supplemental leg; a row enters the pool only when its cosine to the phrase clears
    `memory_search_set_expansion_min_similarity` (a phrase naming an instance the namespace
    never saw finds nothing close and adds nothing), at most
    `memory_search_set_expansion_max_added` rows join, best phrase-cosine first, and none may
    outrank the main leg's best row: raw score = min(phrase cosine, pool max raw). Fail-open."""
    from server.settings import settings as _s

    phrases = list(ctx.set_expansion_phrases)
    if not phrases:
        return
    input = ctx.input
    leg_top_k = max(1, int(_s.memory_search_set_expansion_leg_top_k))
    min_sim = float(_s.memory_search_set_expansion_min_similarity)
    max_added = max(1, int(_s.memory_search_set_expansion_max_added))
    seen_keys = {
        (h.namespace or input.namespace, h.memory_key, h.content_excerpt) for h in ctx.all_hits
    }
    pool_max = max(
        (float(h.score_components.get("rank_score_raw", h.rank_score)) for h in ctx.all_hits),
        default=1.0,
    )
    try:
        per_phrase = await asyncio.gather(
            *[_supplemental_leg(ctx, ph, leg_top_k, seen_keys) for ph in phrases]
        )
    except Exception as e:  # noqa: BLE001 — additive legs
        log.warning("set expansion legs failed (non-fatal): %s", e)
        return
    # A phrase names the question's entity; a row that answers it does too.
    # Without the guard "Maria baked an apple pie" pulls John's pie row into
    # Maria's dessert set, and raw `chunk:` rows (whole turns) outscore the
    # facts they were extracted from.
    ents = [e.lower() for e in _set_expansion.question_entities(str(input.query or ""))]
    candidates: list[Any] = []
    for hits in per_phrase:
        for h in hits:
            if float(h.similarity) < min_sim:
                continue
            key = str(h.memory_key or "")
            if key.startswith("chunk:") or key.startswith("summary:"):
                continue
            if ents:
                hay = (key + " " + str(h.content_excerpt or "")).lower()
                if not any(e in hay for e in ents):
                    continue
            candidates.append(h)
    candidates.sort(key=lambda h: -float(h.similarity))
    added = 0
    for h in candidates[:max_added]:
        raw = min(float(h.similarity), pool_max)
        h.score_components["set_expansion"] = float(h.similarity)
        h.score_components["rank_score_raw"] = raw
        h.rank_score = raw
        ctx.all_hits.append(h)
        added += 1
    ctx.actual_work["set_expansion_added"] = added
    ctx.actual_work["set_expansion_candidates"] = len(candidates)


async def _run_sub_queries(ctx: SearchContext) -> None:
    """Query decomposition. For comparative / multi-entity questions the
    intent parser returns sub_queries targeting each subject; run them as
    supplemental vector searches and merge into the main pool. Dedup is
    content-aware — (namespace, memory_key, excerpt) — because distinct
    facts can share a memory_key and key-only dedup hides all but one."""
    input = ctx.input
    if not ctx.sub_queries:
        return
    ctx.actual_work["sub_query_count"] = len(ctx.sub_queries)
    seen_keys = {
        (h.namespace or input.namespace, h.memory_key, h.content_excerpt) for h in ctx.all_hits
    }
    # Sub-query top_k: wider for aggregational + temporal_diff so each
    # sub-query surfaces enough evidence to aggregate; otherwise divide-by-N.
    if ctx.query_shape in ("aggregational", "temporal_diff"):
        sub_top_k = max(10, input.top_k // 2)
    else:
        sub_top_k = max(5, input.top_k // max(1, len(ctx.sub_queries)))

    sub_results_by_query = await asyncio.gather(
        *[_supplemental_leg(ctx, sq, sub_top_k, seen_keys) for sq in ctx.sub_queries]
    )
    for sub_hits in sub_results_by_query:
        ctx.all_hits.extend(sub_hits)
