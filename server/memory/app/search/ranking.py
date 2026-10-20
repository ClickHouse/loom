"""Ordering the pool: the single global fusion pass, the mention boost, the
chronological sort, and the external rerank."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import cast

from server.core.ch import get_client
from server.core.datetimes import parse_iso_dt
from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.context import SearchContext
from server.memory.app.search.knobs import _mention_boost_weight, _resolve_prior_cap
from server.memory.app.search.scoring import (
    _effective_ann_boosts,
    _effective_arm_boosts,
    _record_rerank_trail,
)
from server.memory.domain.search_types import hit_sort_key

log = logging.getLogger(__package__)


async def _apply_mention_boost(ctx: SearchContext) -> None:
    """After global fusion, hits whose entities the rest of the pool also mentions move up by a
    capped amount, at the request's `mention_boost` weight (unset = off). The legs do not
    select `entities`, so the pool's keys are point-read once from memory_embeddings (as
    structural support does for the final hits); a failed read leaves the pool untouched."""
    weight = _mention_boost_weight(ctx.input)
    if weight <= 0.0 or not ctx.all_hits:
        return
    from server.memory.domain.mention_boost import apply_mention_boost

    entities_by_key: dict[str, list[str]] = {}
    missing = [h.memory_key for h in ctx.all_hits if not getattr(h, "entities", None)]
    if missing:
        try:
            _rows = await get_client().query_all(
                """
                SELECT memory_key, any(entities) AS entities
                FROM loom.memory_embeddings FINAL
                WHERE org = {org:String} AND namespace IN {nss:Array(String)}
                  AND memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
                GROUP BY memory_key
                """,
                {"org": ctx.input.org, "nss": list(ctx.namespaces), "keys": missing},
            )
            entities_by_key = {
                str(r["memory_key"]): [str(e) for e in (r["entities"] or []) if e] for r in _rows
            }
        except Exception as e:  # noqa: BLE001 — a boost, never a failure
            log.warning("memory.search: mention boost entity read failed: %s", e)
            return
    ctx.actual_work["mention_boost_filled"] = sum(1 for v in entities_by_key.values() if v)
    work = apply_mention_boost(
        ctx.all_hits,
        weight=weight,
        cap=_resolve_prior_cap(ctx.input),
        entities_by_key=entities_by_key,
    )
    ctx.actual_work.update(work)


def _apply_global_fusion(ctx: SearchContext) -> None:
    """Single global fusion pass over the COMPLETE candidate pool (main +
    sub-query + anchor arms). Compose the entity-ANN boost onto each arm's
    RAW rank_score and do the one authoritative sort. `_effective_ann_boosts`
    enforces the fusion invariant (no boost exceeds the pool's max raw base)."""
    all_hits = ctx.all_hits
    if not all_hits:
        return
    eff_boosts = _effective_ann_boosts(all_hits, rrf_pool=ctx.input.search_mode == "rrf")
    from server.settings import settings as _fusion_settings

    key_boosts = _effective_arm_boosts(
        all_hits,
        component="key_match",
        rrf_pool=ctx.input.search_mode == "rrf",
        scale=float(_fusion_settings.memory_search_key_match_boost_scale),
    )
    # A hand-asserted fact wins a near-tie against a derived row.
    from server.memory.app.search.scoring import asserted_component
    from server.memory.domain.fact_citations import defer_citing_rows as _defer_citing_rows

    _asserted_scale = float(_fusion_settings.memory_search_asserted_boost_scale)
    asserted_boosts = (
        _effective_arm_boosts(
            all_hits,
            component="asserted",
            rrf_pool=ctx.input.search_mode == "rrf",
            scale=_asserted_scale,
        )
        if _asserted_scale > 0 and asserted_component(all_hits)
        else {}
    )

    for i, h in enumerate(all_hits):
        raw = float(h.score_components.get("rank_score_raw", h.rank_score))
        boost = float(eff_boosts.get(i, 0.0))
        if boost:
            h.score_components["entity_ann_boost_effective"] = boost
        kboost = float(key_boosts.get(i, 0.0))
        if kboost:
            h.score_components["key_match_boost_effective"] = kboost
        aboost = float(asserted_boosts.get(i, 0.0))
        if aboost:
            h.score_components["asserted_boost_effective"] = aboost
        fused = raw + boost + kboost + aboost
        h.score_components["rank_score"] = float(fused)
        h.rank_score = float(fused)
    _defer_citing_rows(all_hits)  # a digest that cites a fact defers to it
    all_hits.sort(key=hit_sort_key)
    # An exact identifier match outranks every neighbour.
    from server.memory.domain.query_planner import identifier_tokens, pin_identifier_hits

    _idents = identifier_tokens(str(getattr(ctx.input, "query", "") or ""))
    if _idents:
        pinned = pin_identifier_hits(all_hits, _idents)
        if pinned:
            ctx.actual_work["identifier_pinned"] = int(pinned)


def _apply_chronological_sort(ctx: SearchContext) -> None:
    """Comparative-order intent: the date IS the answer for "X or Y first?"
    queries. Cosine gives the candidate set; date orders within. Undated
    memories sink to the bottom."""
    if ctx.chronological_intent not in ("asc", "desc"):
        return
    # SQL coalesces a missing date to toDateTime(0); treat that sentinel
    # (and anything <= it) as undated.
    _epoch = datetime(1970, 1, 1)

    def _date_value(h: _MemorySearchHit) -> datetime | None:
        # Prefer valid_at over temporal_anchor. Parse to datetime so
        # ordering is chronological, not lexical — mixed precision /
        # date-only-vs-datetime across the two columns sorts wrong as
        # raw strings.
        dt = parse_iso_dt(h.valid_at) or parse_iso_dt(h.temporal_anchor)
        return None if dt is None or dt <= _epoch else dt

    reverse = ctx.chronological_intent == "desc"
    annotated = [(_date_value(h), h) for h in ctx.all_hits]
    dated_pairs = [p for p in annotated if p[0] is not None]
    # Same-date tiebreak: rank_score DESCENDING in BOTH directions. Two
    # stable passes keep the date sign in `reverse` while leaving the
    # tiebreak untouched — a single `reverse` over a (date, -rank) tuple
    # would invert the tiebreak on desc.
    dated_pairs.sort(key=lambda p: hit_sort_key(p[1]))
    dated_pairs.sort(key=lambda p: cast(datetime, p[0]), reverse=reverse)
    undated = [h for d, h in annotated if d is None]
    ctx.all_hits = [h for _, h in dated_pairs] + undated


async def _run_external_rerank(ctx: SearchContext) -> None:
    """Pluggable rerank: if the namespace has a rerank_endpoint, POST the
    pool to the operator's reranker. Fail-open so a broken reranker degrades
    to cosine ordering. Yields to chronological_intent (the date-sort above
    owns the final order)."""
    from server.memory.app.rerank import RerankCandidate, rerank

    input = ctx.input
    if not (ctx.endpoint and ctx.all_hits and not ctx.chronological_intent):
        return
    ctx.actual_work["external_rerank_used"] = True
    candidates = [
        RerankCandidate(
            memory_key=h.memory_key,
            namespace=h.namespace or input.namespace,
            content_excerpt=h.content_excerpt,
            memory_type=h.memory_type,
            similarity=h.similarity,
        )
        for h in ctx.all_hits
    ]
    reranked = await rerank(
        endpoint=ctx.endpoint,
        prompt_template=ctx.prompt_template,
        query=input.query,
        candidates=candidates,
    )
    # Order-only reranker (cohere / openai / operator HTTP endpoint): no per-candidate
    # score to blend, so honor its ordering directly. Candidates map back to their hits
    # by identity: extraction writes a key once per session, so a key names several hits.
    hit_of = {id(c): h for c, h in zip(candidates, ctx.all_hits, strict=True)}
    before = list(ctx.all_hits)
    ordered: list[_MemorySearchHit] = []
    for c in reranked:
        h = hit_of.pop(id(c), None)
        if h is not None:
            ordered.append(h)
    # The block sorts by rank_score: the pool's own scores, handed out in the reranker's
    # order, make that sort keep it without changing the scores' spread.
    for h, score in zip(
        ordered, sorted((o.rank_score for o in ordered), reverse=True), strict=True
    ):
        h.score_components["rank_score_pre_rerank"] = h.rank_score
        h.rank_score = score
    ctx.all_hits = ordered
    ctx.actual_work.update(_record_rerank_trail(before, ctx.all_hits, "external"))
