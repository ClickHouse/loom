"""The anchor_first / recent_first tiers placed ahead of the similarity hits, the
body-length floor, and the belted finish of the paraphrase path."""

from __future__ import annotations

import json
import logging
from typing import Any

from server.core.tag_sql import TagFilter
from server.identity.app.request_scope import request_scope
from server.memory.app.search_tag_filter import request_hard_tags as _request_hard_tags
from server.memory.app.search_tag_filter import stamp_row_tags

log = logging.getLogger(__package__)


def _apply_min_body_chars(hits: list[Any], input: Any) -> list[Any]:
    """Drop hits whose body is shorter than `min_body_chars`.
    Off at 0. Keeps order; a shorter list is the honest answer, not a
    readmitted filler."""
    n = int(getattr(input, "min_body_chars", 0) or 0)
    if n <= 0 or not hits:
        return hits
    return [h for h in hits if len((getattr(h, "content_excerpt", "") or "").strip()) >= n]


def _reader_user_id(input: Any) -> str:
    """The user axis for the tier and body readers, the way the similarity
    legs read it: a request that names no user — `user_id`
    absent or "-" under read_scope=namespace — reads every user's rows ("*",
    the repository wildcard), so a row written under a caller-chosen user_id
    is not invisible to anchor_first, recent_first and bodies=unit. A named
    user (or a read_scope that pinned one) stays that user."""
    uid = str(getattr(input, "user_id", None) or "").strip()
    return uid if uid and uid != "-" else "*"


def _tier_window(tier_rows: list[Any], hits: list[Any], input: Any) -> list[Any]:
    """Compose a tier (anchor_first / recent_first rows) with the similarity hits: the tier
    sits inside `top_k` and displaces the tail."""
    top_k = int(getattr(input, "top_k", 0) or 0)
    merged = tier_rows + hits
    return merged[:top_k] if top_k else merged


# tier -> the order its rows are read in: the anchor is what a task set out to do (its oldest
# rows), the recent tier what it just did (its newest).
_TIER_ORDER = {"anchor_first": "created_at_asc", "recent_first": "created_at_desc"}


async def _prepend_tier(hits: list[Any], input: Any, tier: str) -> list[Any]:
    """Run-scoped tier: place the request's `anchor_first` oldest / `recent_first` newest
    memories of this identity (and of `run_id` when the request names one) ahead of the
    similarity hits, de-duplicated, within top_k. Rows carry score_components[tier] = 1.0 and
    rank_score 0, so a consumer can tell the tier from a match. Fail-open."""
    n = int(getattr(input, tier, 0) or 0)
    if n <= 0:
        return hits
    try:
        from server.memory.api.models_search import _MemorySearchHit
        from server.memory.store import Identity, list_by_namespace

        ident = Identity(
            org=input.org,
            namespace=input.namespace,
            agent=str(getattr(input, "agent", "*") or "*"),
            user_id=_reader_user_id(input),
            session_id=str(getattr(input, "session_id", None) or "-"),
        )
        run_id = str(getattr(input, "run_id", "") or "")
        hard_tags = _request_hard_tags(input)
        rows = await list_by_namespace(
            ident,
            order_by=_TIER_ORDER[tier],
            limit=n * 4 if run_id else n,
            # The request's hard filter, matched exactly as every memory.search leg does.
            tag_filter=TagFilter(tuple(hard_tags), "exact_any") if hard_tags else None,
            scope=request_scope(),
            # A 'high' row is never in a search leg; it is readable by key only.
            exclude_high_sensitivity=True,
        )
    except Exception as e:  # noqa: BLE001 — a tier never fails a search
        log.debug("%s tier read failed: %s", tier, e)
        return hits
    if run_id:
        rows = [r for r in rows if str(getattr(r, "run_id", "") or "") == run_id]
    rows = rows[:n]
    have = {str(getattr(h, "memory_key", "")) for h in hits}
    tier_rows: list[Any] = []
    for r in rows:
        key = str(getattr(r, "memory_key", ""))
        if not key or key in have:
            continue
        val = getattr(r, "value", "")
        text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, default=str)
        try:
            hit = _MemorySearchHit(
                memory_key=key,
                memory_type=str(getattr(r, "memory_type", "semantic") or "semantic"),
                content_excerpt=text[:1200],
                distance=1.0,
                similarity=0.0,
                rank_score=0.0,
                created_at=str(getattr(r, "created_at", "") or ""),
                score_components={tier: 1.0},
            )
        except Exception:  # noqa: BLE001 — a row the hit model refuses is left out
            continue
        who = [getattr(r, c, None) for c in ("agent", "user_id", "session_id")]
        if None not in who:
            hit._row_identity = (str(who[0]), str(who[1]), str(who[2]))
        # `_apply_bodies` and the hard tag belt read them: a constraint keeps its excerpt,
        # and the belt needs no read of its own.
        tier_rows.append(stamp_row_tags(hit, getattr(r, "tags", None)))
    # the tiers join after the pool's code annotation: theirs is read here
    from server.memory.app.code_link_reads import annotate_hits

    await annotate_hits(input, tier_rows)
    return _tier_window(tier_rows, hits, input)


async def _prepend_anchor(hits: list[Any], input: Any) -> list[Any]:
    return await _prepend_tier(hits, input, "anchor_first")


async def _prepend_recent(hits: list[Any], input: Any) -> list[Any]:
    return await _prepend_tier(hits, input, "recent_first")
