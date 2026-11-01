"""Assembling the served block from the fused pool: the side legs fold in, the
pool is trimmed, and the context-block assembler cuts the window."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Any

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.retrieval.pool import folded_fields
from server.memory.app.search.anchor_pool import _enforce_agent_scope
from server.memory.app.search.context import SearchContext
from server.memory.app.search.digest_descent import (
    _run_digest_descent,
    digest_descent_enabled,
)
from server.memory.app.search.scoring import rows_per_key
from server.memory.app.search.telemetry import _staged
from server.memory.app.search.vector_legs import _plane_fields, _stamp_leg_tags
from server.memory.app.search_tag_filter import enforce_hard_tag_filter as _enforce_hard_tag_filter
from server.memory.app.served_duplicates import hide_pool_duplicates
from server.memory.app.served_superseded import annotate_pool_superseded
from server.memory.domain.search_types import hit_row_key, hit_writer, names_row

log = logging.getLogger(__package__)


def _drop_excluded_keys(ctx: SearchContext) -> None:
    """Keys the caller already holds leave the pool after every
    leg has folded in and before the window is cut, so the window is still
    `top_k` long — filled from the next-best candidates."""
    excl = {k for k in (getattr(ctx.input, "exclude_keys", None) or []) if k}
    if not excl:
        return
    before = len(ctx.all_hits)
    ctx.all_hits = [h for h in ctx.all_hits if str(getattr(h, "memory_key", "") or "") not in excl]
    ctx.actual_work["excluded_keys_dropped"] = before - len(ctx.all_hits)


def fold_writer_hits(hits: list[_MemorySearchHit], namespace: str) -> list[_MemorySearchHit]:
    """Fold each set chunk that names only its writer onto that writer's newest pooled row
    of the key, as `fold_set_chunks` folds a chunk onto a pooled parent, at the earlier of
    the two positions. A key's chunks hold the writer's newest value; when that row is not
    pooled, an older pooled row of the writer takes the passage."""
    if not any(hit_writer(h) for h in hits):
        return hits
    rows: dict[tuple[str, str, str, str], _MemorySearchHit] = {}
    for h in hits:
        key = hit_row_key(h, namespace).writer_key()
        if names_row(h) and (key not in rows or h.version > rows[key].version):
            rows[key] = h
    out: list[_MemorySearchHit] = []
    placed: set[int] = set()
    for h in hits:
        row = rows.get(hit_row_key(h, namespace).writer_key()) if hit_writer(h) else None
        if row is not None:
            fields = folded_fields(row, h)
            for name, value in fields.items():
                setattr(row, name, value)
            if "rank_score" in fields:
                row.similarity = max(row.similarity, h.similarity)
        target = row or h
        if id(target) not in placed:
            placed.add(id(target))
            out.append(target)
    return out


async def _drop_excluded_and_duplicates(ctx: SearchContext) -> None:
    """The pool trims that run after every leg folded in and before the window is cut: the
    caller's `exclude_keys`, the `duplicate` rows and their canonicals, then the hard tag belt —
    last, because the canonical a duplicate is exchanged for entered the pool through no leg's
    WHERE and is the row the belt exists to judge; on the whole fused pool, so a dropped row
    leaves no hole in top_k and a parked page's tail is as clean as the first page."""
    _drop_excluded_keys(ctx)
    ctx.all_hits = fold_writer_hits(ctx.all_hits, ctx.input.namespace)
    if getattr(ctx.input, "quality_filter", False):
        from server.memory.domain.fact_quality import drop_junk_and_restatements

        ctx.all_hits, _dropped = drop_junk_and_restatements(ctx.all_hits)
        if _dropped:
            ctx.actual_work["quality_dropped"] = _dropped
    await hide_pool_duplicates(ctx)
    await annotate_pool_superseded(ctx)  # name the successor when asked
    ctx.all_hits = await _enforce_hard_tag_filter(
        ctx.all_hits, ctx.input, ctx.actual_work, list(ctx.namespaces)
    )
    # No anchor row of another agent's private scope either.
    ctx.all_hits = _enforce_agent_scope(ctx.all_hits, ctx.input, ctx.actual_work)
    # Nor a memory about code that is gone: the window and the verdict see what is served.
    from server.memory.app.code_link_reads import annotate_hits

    pooled = len(ctx.all_hits)
    await annotate_hits(ctx.input, ctx.all_hits)
    if pooled != len(ctx.all_hits):
        ctx.actual_work["code_removed_dropped"] = pooled - len(ctx.all_hits)


def _ledger_key_axis(ctx: Any, input: Any, settings_obj: Any) -> None:
    """The chunk rows the bounded lift moved, and which way the key axis went and why, on the
    ledger."""
    # How many chunk rows the assembler's bounded lift moved
    # (`chunk_parent_lift` in score_components); absent when none.
    _bounded = sum(1 for h in ctx.all_hits if "chunk_parent_lift" in (h.score_components or {}))
    if _bounded:
        ctx.actual_work["chunk_lift_bounded"] = int(_bounded)
    from server.memory.app.retrieval import key_axis_auto_cached

    ka_req = getattr(input, "key_axis", None)
    if ka_req is not None:
        ctx.actual_work["key_axis"] = "on" if ka_req else "off"
    else:
        ka_mode = settings_obj.memory_search_key_axis
        if ka_mode == "auto":
            first_ns = str(list(ctx.namespaces)[0]) if ctx.namespaces else ""
            decided = key_axis_auto_cached(input.org, first_ns, settings_obj)
            if decided is not None:
                ctx.actual_work["key_axis"] = "on" if decided[0] else "off"
                ctx.actual_work["key_axis_reused_share"] = round(float(decided[1]), 3)
        else:
            ctx.actual_work["key_axis"] = ka_mode


async def _assemble_final_hits(
    ctx: SearchContext,
    summary_task: asyncio.Task[Any],
    observation_task: asyncio.Task[Any] | None,
    entity_summary_task: asyncio.Task[Any] | None = None,
) -> None:
    """Additive context-block assembler: the fact pool keeps its baseline
    relevance order sliced to top_k, session summaries fold in as additive
    candidates (normalised into the pool's score band, never dropping a
    fact), and observations/derived/computed are PINNED on top. Only
    exact-duplicate excerpts collapse here."""
    from server.memory.domain.context_block import (
        DEFAULT_BLOCK_TOKEN_BUDGET,
        assemble_context_block,
    )

    input = ctx.input

    def _summary_hit_to_wire(ns: str, h: Any) -> _MemorySearchHit:
        return _stamp_leg_tags(
            h,
            _MemorySearchHit(
                memory_key=h.memory_key,
                memory_type=getattr(h, "memory_type", "semantic") or "semantic",
                content_excerpt=h.content_excerpt,
                distance=float(getattr(h, "distance", 1.0)),
                similarity=1.0 - float(getattr(h, "distance", 1.0)),
                rank_score=float(getattr(h, "rank_score", 0.0) or 0.0),
                temporal_anchor=getattr(h, "temporal_anchor", "") or "",
                temporal_confidence=float(getattr(h, "temporal_confidence", 0.0) or 0.0),
                valid_at=getattr(h, "valid_at", "") or "",
                **_plane_fields(h),
                status=str(getattr(h, "status", "") or ""),
                version=int(getattr(h, "version", 0) or 0),
                namespace=ns,
                score_components=dict(getattr(h, "score_components", {}) or {}),
            ),
        )

    try:
        for ns, sh in await summary_task:
            ctx.all_hits.append(_summary_hit_to_wire(ns, sh))
    except Exception as e:  # noqa: BLE001
        log.warning("session-summary leg failed (non-fatal): %s", e)
    # Fold the PRE-BUILT entity summaries in as additive dense rows (source="entity_summary";
    # calibrated into the pool band by the assembler, never displacing a fact). Default OFF →
    # task is None → no-op.
    if entity_summary_task is not None:
        try:
            for ns, eh in await entity_summary_task:
                ctx.all_hits.append(_summary_hit_to_wire(ns, eh))
        except Exception as e:  # noqa: BLE001
            log.warning("entity-summary leg failed (non-fatal): %s", e)
    if observation_task is not None:
        try:
            for ns, oh in await observation_task:
                ctx.all_hits.append(_summary_hit_to_wire(ns, oh))
        except Exception as e:  # noqa: BLE001
            log.warning("observation leg failed (non-fatal): %s", e)
    # Digest-anchored expansion: the sessions behind the top-m digests contribute their own
    # top-k facts as candidates. The assembly so far is charged to `assemble` first, so the
    # descent's statements name their own stage; the orchestrator's closing mark adds the rest
    # of the assembly to `assemble` again.
    ctx.clock.mark("assemble")
    await _staged(
        ctx,
        "digest_descent",
        _run_digest_descent(ctx),
        active=bool(digest_descent_enabled(input) and getattr(input, "recipe", "") != "fast"),
    )
    ctx.clock.mark("digest_descent")

    max_block_tokens = DEFAULT_BLOCK_TOKEN_BUDGET
    if getattr(input, "_context_pack_block_tokens", 0):
        # context.pack threads its per-section budget down so pack's existing
        # token_budget governs the curated block there.
        max_block_tokens = int(input._context_pack_block_tokens)

    from server.settings import settings as _asm_settings

    per_key_cap = rows_per_key(ctx.query_shape)
    # Per-speaker quota (retrieve per conversation participant), per request; 0 = off.
    per_speaker_quota = int(getattr(input, "per_speaker_quota", None) or 0)
    # Per-session cap, per request; aggregational shapes want every row and take no cap.
    per_session_cap = _diversify_session_cap(input, ctx.query_shape)
    # Digest cap, per request; 0 keeps every digest the band admits.
    summary_cap = int(getattr(input, "summary_cap", None) or 0)
    await _drop_excluded_and_duplicates(ctx)
    final_hits = assemble_context_block(
        ctx.all_hits,
        top_k=int(getattr(ctx.input, "top_k", 0) or 0),
        max_block_tokens=max_block_tokens,
        per_key_cap=per_key_cap,
        per_speaker_quota=per_speaker_quota,
        per_session_cap=per_session_cap,
        summary_cap=summary_cap,
        bound_component_lifts=ctx.input.search_mode != "rrf",  # bounded lifts; not under rrf
    )
    # Fact rows first: verbatim chunks hold at most the request's share of the block; surplus
    # chunks give their slots to the best stored fact rows the window did not reach. 0 = off.
    chunk_cap_share = float(getattr(input, "chunk_cap_share", None) or 0.0)
    if chunk_cap_share > 0:
        from server.memory.domain.context_block import cap_chunk_share as _cap_chunks

        final_hits, _replaced = _cap_chunks(
            final_hits, ctx.all_hits, top_k=int(input.top_k), max_share=chunk_cap_share
        )
        ctx.actual_work["chunk_cap_replaced"] = int(_replaced)
    # Provenance backfill: a chunk or summary hit's key encodes the session it came from; put
    # it on the wire so a caller or a bench can score source recall on every hit, not only
    # facts.
    from server.memory.domain.context_block import session_of_hit as _session_of

    for h in final_hits:
        if not getattr(h, "session_id", ""):
            _sid = _session_of(h)
            if _sid:
                try:
                    h.session_id = _sid
                except Exception:  # noqa: BLE001 — frozen or foreign hit types stay as they are
                    pass
    if per_session_cap > 0:
        ctx.actual_work["diversify_session_cap"] = int(per_session_cap)
    # The key axis and the chunk lift on the ledger.
    try:
        _ledger_key_axis(ctx, input, _asm_settings)
    except Exception:  # noqa: BLE001 — ledger only
        pass
    ctx.actual_work["summary_cap"] = summary_cap
    ctx.actual_work["block_tokens"] = sum(len(h.content_excerpt) // 4 for h in final_hits)
    ctx.actual_work["items_by_source"] = dict(
        Counter(getattr(h, "source", "fact") or "fact" for h in final_hits)
    )
    ctx.final_hits = final_hits
    # What the block did not show. Distinct pool keys are the "of 124"; the ranked remainder
    # is what a `next_page` handle serves.
    from server.memory.app.search_pages import split_tail as _split_tail

    ctx.candidates_total, ctx.page_tail = _split_tail(ctx.all_hits, final_hits)
    ctx.actual_work["candidates_total"] = int(ctx.candidates_total)


def _diversify_session_cap(input: Any, query_shape: str) -> int:
    """Per-session cap for the block, 0 = off. On only when the request's
    `diversify` is "session"; the cap is the request's, else max(2, top_k // 3).
    Aggregational and temporal-difference shapes take no cap: they want every
    row."""
    mode = str(getattr(input, "diversify", "") or "")
    if mode != "session" or query_shape in ("aggregational", "temporal_diff"):
        return 0
    cap = getattr(input, "diversify_session_cap", None) or 0
    if int(cap) <= 0:
        cap = max(2, int(getattr(input, "top_k", 10) or 10) // 3)
    return int(cap)
