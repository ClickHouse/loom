"""The entity / temporal anchor pool, and the belt that keeps another agent's
private rows out of the fused pool."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.context import SearchContext
from server.memory.app.search.scoring import _anchor_rank_score
from server.memory.app.search.sql import _anchor_pool_where, anchor_pool_sql
from server.memory.app.search_tag_filter import stamp_row_tags as _stamp_row_tags
from server.memory.domain.search_sql import agent_filter
from server.memory.domain.search_types import hit_row_key, row_identity, row_key_of

log = logging.getLogger(__package__)


def _stamp_row_owner[HitT](hit: HitT, agent: Any, scope: Any) -> HitT:
    """Remember the stored row's agent and scope on the wire hit, privately, as the anchor pool
    read them; the belt below checks the request's agent against them without a read."""
    try:
        hit._row_agent = str(agent or "")  # type: ignore[attr-defined]
        hit._row_scope = str(scope or "")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a foreign hit type stays as it is
        pass
    return hit


def _enforce_agent_scope(hits: list[Any], input: Any, work: dict[str, Any]) -> list[Any]:
    """The belt: a request naming an agent gets no anchor row
    of another agent's private scope. The anchor pool's statement carries the
    clause, so this drops nothing on a healthy path; it is here so a leg that
    stamps an owner and serves past it is caught and disclosed
    (`retrieval_work.agent_filter_dropped`, written only when a row fell).
    Judges stamped rows only, in process — no statement — and leaves every
    hit untouched under a cross-agent request (`*`, `-`, none)."""
    agent = agent_filter(getattr(input, "agent", ""))
    if not agent or not hits:
        return hits
    kept: list[Any] = []
    dropped = 0
    for h in hits:
        owner = getattr(h, "_row_agent", None)
        if owner is None or owner == agent or getattr(h, "_row_scope", "") == "shared":
            kept.append(h)
        else:
            dropped += 1
    if dropped:
        work["agent_filter_dropped"] = int(work.get("agent_filter_dropped", 0)) + dropped
        log.warning(
            "memory.search: %d fused row(s) belonged to another agent's private scope "
            "and were dropped (a leg served past its agent predicate)",
            dropped,
        )
    return kept


async def _run_anchor_pool(ctx: SearchContext) -> None:
    """Entity-anchor + temporal-anchor additive pool: use query-resolved
    entities and dates as HARD ANCHORS to pull in candidates the dense ANN
    under-weighted. ADDITIVE (OR-merge into the pool, not a filter) so
    synonym / paraphrase misses on the anchors don't crash recall."""
    input = ctx.input
    if not (
        (ctx.effective_entity_filter or ctx.resolved_dates)
        and getattr(input, "recipe", "") != "fast"
    ):
        return
    # one key holds a row per writer and session: skip only the rows already pooled
    anchor_seen = {hit_row_key(h, input.namespace) for h in ctx.all_hits}
    # Anchor rows enter strictly below the weakest pooled candidate
    # (see _anchor_rank_score); the floor is the pool's minimum raw
    # base score at injection time.
    _pool_floor = min(
        (float(h.score_components.get("rank_score_raw", h.rank_score)) for h in ctx.all_hits),
        default=0.0,
    )
    try:
        from server.core.ch import get_client as _get_ch_a

        _ch_a = _get_ch_a()
        _anchor_limit = max(30, input.top_k * 2)

        # One CH round-trip per namespace; the legs are independent,
        # so they run in one gather instead of a sequential loop.
        async def _anchor_rows_for_ns(ns: str) -> tuple[str, list[dict[str, Any]]]:
            params: dict[str, Any] = {
                "org": input.org,
                "ns": ns,
                "limit": _anchor_limit,
                "model": ctx.embedder.model_name,
            }
            # identity + `e.run_id = {run_id}` under a run
            where_parts = _anchor_pool_where(input, params)
            if ctx.effective_entity_filter:
                params["ents"] = [t.lower() for t in ctx.effective_entity_filter]
            if ctx.resolved_dates:
                params["anchor_dates"] = list(ctx.resolved_dates)
            # OR of the two anchor predicates. If only one is set,
            # the other is dropped at SQL compose time.
            anchor_preds = []
            if ctx.effective_entity_filter:
                anchor_preds.append("hasAny(e.entities, {ents:Array(String)})")
            if ctx.resolved_dates:
                anchor_preds.append(
                    "arrayExists(d -> "
                    "  (e.temporal_anchor IS NOT NULL AND "
                    "   abs(dateDiff('day', e.temporal_anchor, "
                    "       parseDateTimeBestEffortOrNull(d))) <= 14) "
                    "  OR (e.valid_at IS NOT NULL AND "
                    "      abs(dateDiff('day', e.valid_at, "
                    "          parseDateTimeBestEffortOrNull(d))) <= 14), "
                    "{anchor_dates:Array(String)})"
                )
            where_sql = " AND ".join(where_parts) + " AND (" + " OR ".join(anchor_preds) + ")"
            anchor_sql = anchor_pool_sql(where_sql)
            params["qv"] = ctx.query_vec
            try:
                return ns, await _ch_a.query_all(anchor_sql, params=params)
            except Exception as e:  # noqa: BLE001 — one namespace's anchor pool failure must not fail search
                log.warning("anchor pool fetch failed for ns=%s (non-fatal): %s", ns, e)
                return ns, []

        anchor_results = await asyncio.gather(*[_anchor_rows_for_ns(ns) for ns in ctx.namespaces])
        for ns, anchor_rows in anchor_results:
            for row in anchor_rows:
                mk = str(row.get("memory_key") or "")
                excerpt = str(row.get("content_excerpt") or "")
                if not mk:
                    continue
                key = row_key_of(ns, row)
                if key in anchor_seen:
                    continue
                anchor_seen.add(key)
                sim = float(row.get("similarity") or 0.0)
                # Anchor hits are recall candidates, not ranked evidence: they enter strictly
                # below the weakest main-pool candidate (ordered among themselves by similarity)
                # so they can only fill the tail unless a boost or the rerank promotes them.
                hit = _stamp_row_tags(
                    _MemorySearchHit(
                        memory_key=mk,
                        memory_type=str(row.get("memory_type") or "semantic"),
                        version=int(row.get("version") or 0),
                        content_excerpt=excerpt,
                        distance=1.0 - sim,
                        similarity=sim,
                        rank_score=_anchor_rank_score(sim, _pool_floor),
                        temporal_anchor=str(row.get("temporal_anchor") or ""),
                        temporal_confidence=float(row.get("temporal_confidence") or 0.0),
                        valid_at=str(row.get("valid_at") or ""),
                        source="anchor",
                        session_id=str(row.get("session_id") or ""),
                        source_resource_id=str(row.get("source_resource_id") or ""),
                        namespace=ns,
                    ),
                    row.get("tags"),
                )
                hit._row_identity = row_identity(row)
                ctx.all_hits.append(_stamp_row_owner(hit, row.get("agent"), row.get("scope")))
    except Exception as e:  # noqa: BLE001 — the anchor pool is an optional recall stage
        log.warning("anchor-pool stage failed (non-fatal): %s", e)
