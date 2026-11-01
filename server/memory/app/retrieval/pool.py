"""The fused pool's last steps: folding memory.set chunks onto their parent
fact, the tag-scoped recency backfill and the recency re-rank, and the
chunk-share cap and floor on the window.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from server.memory.domain.search_parsing import _iso_date
from server.memory.domain.search_sql import _build_where, exclude_pooled
from server.memory.domain.search_types import (
    SearchHit,
    SearchQuery,
    _CHClient,
    hit_row_key,
    hit_sort_key,
    hit_writer,
    names_row,
    row_identity,
)

log = logging.getLogger("server.memory.app.retrieval")


async def _recency_backfill(
    ch: _CHClient, q: SearchQuery, hits: list[SearchHit]
) -> list[SearchHit]:
    """On a recency-intent read that a hard tag
    scopes and a pool weight orders, the newest `top_k` rows in scope join
    the pool before the re-rank. "What did the latest run of X find?" over a
    workflow with nine thousand runs has cosine neighbours that are recent
    rows, not the newest — the re-rank can only order what the legs
    fetched, so the newest run never reaches it. The scope is the subject here, so the newest rows in
    it are candidates by construction; without a hard tag the read is left
    alone. Rows already in the pool are excluded; the new rows enter at the
    pool's lowest rank score — in scope, so as good as the weakest candidate
    — and win only through the recency boost. Fail-open."""
    weight = float(q.recency_pool_weight)
    if not (q.recency_intent and weight > 0.0 and int(q.top_k) > 0):
        return hits
    from server.memory.domain.search_sql import hard_tag_filter

    if not hard_tag_filter(q.tags, q.tags_soft):
        return hits
    where, params = _build_where(q)
    exclude_pooled(where, params, hits)
    params["newest_limit"] = int(q.top_k)
    sql = f"""
        SELECT
            e.memory_key       AS memory_key,
            e.memory_type      AS memory_type,
            e.content_excerpt  AS content_excerpt,
            cosineDistance(e.embedding, {{q:Array(Float32)}}) AS distance,
            toString(coalesce(e.temporal_anchor, toDateTime(0))) AS temporal_anchor,
            e.temporal_confidence AS temporal_confidence,
            toString(coalesce(e.valid_at, toDateTime(0))) AS valid_at,
            toString(e.created_at) AS created_at,
            ifNull(e.source_resource_id, '') AS source_resource_id,
            e.source_class AS source_class,
            e.verified AS verified,
            e.agent AS agent,
            e.user_id AS user_id,
            e.session_id AS session_id,
            e.tags AS tags
        FROM loom.memory_embeddings AS e FINAL
        WHERE {" AND ".join(where)}
        ORDER BY coalesce(e.valid_at, e.temporal_anchor, e.created_at) DESC, e.memory_key ASC
        LIMIT {{newest_limit:UInt32}}
    """
    try:
        rows = await ch.query_all(sql, params=params)
    except Exception as e:  # noqa: BLE001 — an additive leg fails open
        log.warning("recency backfill failed (fail-open): %s", e)
        return hits
    floor = min((float(h.rank_score or 0.0) for h in hits), default=0.0)
    out: list[SearchHit] = []
    for r in rows:
        out.append(
            SearchHit(
                memory_key=str(r["memory_key"]),
                memory_type=str(r["memory_type"]),
                content_excerpt=str(r["content_excerpt"]),
                distance=float(r["distance"]),
                rank_score=floor,
                temporal_anchor=_iso_date(str(r.get("temporal_anchor", ""))),
                temporal_confidence=float(r.get("temporal_confidence") or 0.0),
                valid_at=_iso_date(str(r.get("valid_at", ""))),
                created_at=str(r.get("created_at") or ""),
                source="fact",
                source_resource_id=str(r.get("source_resource_id") or ""),
                source_class=str(r.get("source_class") or ""),
                verified=bool(int(r.get("verified") or 0)),
                session_id=str(r.get("session_id") or ""),
                identity=row_identity(r),
                tags=[str(t) for t in (r.get("tags") or []) if t],
                score_components={"recency_backfill": 1.0, "rank_score": float(floor)},
            )
        )
    return hits + out


_SET_CHUNK_PREFIX = "chunk:memory.set:"


def _is_set_chunk(h: Any) -> bool:
    return (
        (getattr(h, "source", "") or "") == "chunk"
        and bool(getattr(h, "parent_memory_key", "") or "")
        and str(getattr(h, "memory_key", "") or "").startswith(_SET_CHUNK_PREFIX)
    )


def fold_set_chunks(hits: list[SearchHit]) -> list[SearchHit]:
    """Fold the verbatim chunks of long memory.set values onto
    their parent fact, so a fact-only namespace never sees a chunk id.

    A chunk whose parent is already in the pool gives the parent the better of
    the two scores and, when the chunk out-scored the parent, its passage as
    the excerpt — the parent's stored excerpt is only the value's head, and
    the passage that matched must survive the fold for memory.ask's window.
    A chunk whose parent is not in the pool becomes the parent's hit: keyed
    by the parent, `source="fact"`, the chunk's passage, scores and dates,
    tags empty (the handler's tag point-read fills fact hits by key when the
    request asks). Several chunks of one parent collapse to one row. Order is
    re-established by `hit_sort_key`. Conversation chunks (no memory.set
    session) pass through untouched.
    `score_components` records the fold: `folded_chunks` (count), for a
    converted hit `folded_from_chunk` (the chunk index), and `folded_lift`
    when the row's score is a chunk's — downstream fact-only boosts (the
    asserted tie-break) skip such a row, so the fold is
    rank-neutral: a folded parent sits exactly where its chunk sat."""
    if not hits or not any(_is_set_chunk(h) for h in hits):
        return hits
    # A chunk folds onto its writer's row of the key. Its session is `memory.set:<key>`,
    # not the row's, so it matches on namespace, agent, user_id and key alone.
    by_row: dict[tuple[str, ...], SearchHit] = {}
    out: list[SearchHit] = []
    for h in hits:
        if not _is_set_chunk(h):
            out.append(h)
            if (getattr(h, "source", "") or "fact") != "chunk":
                by_row.setdefault(hit_row_key(h).writer_key(), h)
    for h in hits:
        if not _is_set_chunk(h):
            continue
        parent_key = str(h.parent_memory_key)
        parent_row = hit_row_key(h)._replace(memory_key=parent_key).writer_key()
        parent = by_row.get(parent_row)
        if parent is None:
            idx_s = str(h.memory_key).rpartition(":")[2]
            # the chunk's session is not its row's: the hit keeps only its writer
            converted = replace(
                h,
                memory_key=parent_key,
                source="fact",
                parent_memory_key="",
                identity=None,
                writer=h.identity[:2] if h.identity else None,
                session_id="",
                score_components={
                    **dict(h.score_components or {}),
                    "folded_chunks": 1.0,
                    "folded_from_chunk": float(idx_s) if idx_s.isdigit() else -1.0,
                    # the score is the chunk's: no fact-only boost may be added
                    # downstream (asserted tie-break) — the chunk never had it
                    "folded_lift": 1.0,
                },
            )
            by_row[parent_row] = converted
            out.append(converted)
            continue
        components = dict(parent.score_components or {})
        components["folded_chunks"] = float(components.get("folded_chunks", 0.0)) + 1.0
        if float(h.rank_score or 0.0) > float(parent.rank_score or 0.0):
            components["folded_lift"] = 1.0
            merged = replace(
                parent,
                rank_score=float(h.rank_score),
                distance=min(float(parent.distance), float(h.distance)),
                content_excerpt=h.content_excerpt or parent.content_excerpt,
                score_components=components,
            )
        else:
            merged = replace(parent, score_components=components)
        by_row[parent_row] = merged
        out[out.index(parent)] = merged
    out.sort(key=hit_sort_key)
    return out


def fold_writer_hits(hits: list[Any], namespace: str = "") -> list[Any]:
    """Fold each set chunk that names only its writer onto its writer's row of the key once
    a later leg pooled that row, as `fold_set_chunks` folds a chunk onto a pooled parent: the
    row takes the better score and, when the chunk out-scored it, the chunk's passage, and
    sits where the higher of the two sat."""
    if not any(hit_writer(h) for h in hits):
        return hits
    rows: dict[tuple[str, str, str, str], Any] = {}
    for h in hits:
        if names_row(h):
            rows.setdefault(hit_row_key(h, namespace).writer_key(), h)
    out: list[Any] = []
    placed: set[int] = set()
    for h in hits:
        row = rows.get(hit_row_key(h, namespace).writer_key()) if hit_writer(h) else None
        if row is not None:
            _fold_onto(row, h)
        target = row if row is not None else h
        if id(target) not in placed:
            placed.add(id(target))
            out.append(target)
    return out


def _fold_onto(row: Any, chunk: Any) -> None:
    components = row.score_components
    components["folded_chunks"] = float(components.get("folded_chunks", 0.0)) + 1.0
    if float(chunk.rank_score or 0.0) > float(row.rank_score or 0.0):
        row.rank_score = chunk.rank_score
        row.content_excerpt = chunk.content_excerpt or row.content_excerpt
        components["folded_lift"] = 1.0


async def _finish_pool(ch: _CHClient, hits: list[SearchHit], q: SearchQuery) -> list[SearchHit]:
    """The fused pool's last steps, once per namespace: the newest rows in a
    tag-scoped recency read join the pool (`_recency_backfill`), the recency
    pool re-rank when the query carries a recency cue and
    the namespace (or request) set a weight — the SQL term 0.25/(1+age_years)
    tips ties between days, not months — then the chunk share cap and floor
    and the window cut. No LLM rerank here: it runs
    once, in the handler, on the cross-namespace pool."""
    hits = fold_set_chunks(hits)
    hits = await _recency_backfill(ch, q, hits)
    if q.recency_intent and float(q.recency_pool_weight) > 0.0:
        hits = rerank_by_recency_pool(hits, weight=float(q.recency_pool_weight))
    from server.settings import settings as _cap_settings

    # The request's (or namespace's) chunk share cap; unset = no cap.
    share = 1.0 if q.chunk_max_share is None else float(q.chunk_max_share)
    hits = cap_chunk_share(hits, int(q.top_k), share)
    hits = floor_chunk_share(
        hits,
        int(q.top_k),
        effective_chunk_floor(float(_cap_settings.memory_search_chunk_min_share), share),
    )
    return hits[: int(q.top_k)]


def effective_chunk_floor(min_share: float, max_share: float) -> float:
    """The chunk floor a window honours: the deployment floor,
    never above the cap the request or namespace set (`chunk_max_share`). A
    floor applied after the cap would promote chunks back into a window the
    caller had capped at zero, so `chunk_max_share=0` would not be a fact-only
    read."""
    return max(0.0, min(float(min_share), float(max_share)))


def _hit_date_key(h: Any) -> str:
    """A sortable date for one hit: `valid_at`, else the event anchor, else
    `created_at`, normalised to `YYYY-MM-DD HH:MM:SS`; '' when undated (the
    epoch sentinel the chunk leg emits for an undated row counts as undated)."""
    for attr in ("valid_at", "temporal_anchor", "created_at"):
        raw = str(getattr(h, attr, "") or "").strip().replace("T", " ")
        if raw and not raw.startswith("1970-01-01"):
            return raw[:19]
    return ""


def recency_pool_scores(hits: Sequence[Any], *, weight: float) -> list[float]:
    """Rank-relative recency over one pool, one boost per
    input position. The newest dated hit gains `weight`, each older date
    half of the one before it; hits sharing a date share a rank; an undated
    hit gains nothing. A pool with fewer than two dated hits has no order
    to express and gains nothing. Pure."""
    dated = [(i, _hit_date_key(h)) for i, h in enumerate(hits)]
    dated = [(i, d) for i, d in dated if d]
    distinct = sorted({d for _i, d in dated}, reverse=True)
    if weight <= 0.0 or len(distinct) < 2:
        return [0.0] * len(hits)
    # Geometric by date rank: the newest date gains `weight`, the next half of
    # it, and so on. A linear form gives
    # adjacent dates 0.3/19 = 0.016 apart on a twenty-date pool — under the
    # rank-fusion spread — so the newest run can lose to an older row by 0.008.
    rank_of = {d: r for r, d in enumerate(distinct)}
    boost = {i: weight * (0.5 ** rank_of[d]) for i, d in dated}
    return [boost.get(i, 0.0) for i in range(len(hits))]


def rerank_by_recency_pool(hits: list[SearchHit], *, weight: float) -> list[SearchHit]:
    """The pool with `recency_pool_scores` folded into `rank_score` (kept on
    `score_components["recency_pool"]`) and re-sorted by the one ordering.
    Distances are untouched."""
    boosts = recency_pool_scores(hits, weight=weight)
    if not any(boosts):
        return hits
    out: list[SearchHit] = []
    for h, b in zip(hits, boosts, strict=True):
        if b <= 0.0:
            out.append(h)
            continue
        out.append(
            replace(
                h,
                rank_score=float(h.rank_score or 0.0) + b,
                score_components={**h.score_components, "recency_pool": float(b)},
            )
        )
    out.sort(key=hit_sort_key)
    return out


def floor_chunk_share(hits: list[SearchHit], top_k: int, min_share: float) -> list[SearchHit]:
    """The mirror of `cap_chunk_share` — verbatim-chunk hits
    keep at least `min_share` of the first `top_k` slots when the pool has
    that many chunks. The best-ranked chunks beyond the window move up and
    displace the lowest-ranked non-chunk rows, which follow after the window
    so a larger `top_k` still reaches them. Order inside each group is kept.
    `min_share <= 0` is a no-op; a pool with too few chunks keeps them all."""
    if min_share <= 0.0 or top_k <= 0 or not hits:
        return hits

    def _is_chunk(h: SearchHit) -> bool:
        # A parent that a memory.set chunk folded onto IS the passage match
        # the floor exists to keep; it keeps the chunk's seat. Without this
        # the floor would promote long lessons through their chunks.
        return (
            (getattr(h, "source", "") or "") == "chunk"
            or str(h.memory_key).startswith("chunk:")
            or float((getattr(h, "score_components", None) or {}).get("folded_chunks", 0.0)) > 0.0
        )

    window = list(hits[:top_k])
    rest = list(hits[top_k:])
    have = sum(1 for h in window if _is_chunk(h))
    need = min(int(top_k * min_share), have + sum(1 for h in rest if _is_chunk(h))) - have
    if need <= 0:
        return hits
    promote: list[SearchHit] = []
    kept_rest: list[SearchHit] = []
    for h in rest:
        if len(promote) < need and _is_chunk(h):
            promote.append(h)
        else:
            kept_rest.append(h)
    # Displace the lowest-ranked non-chunk rows of the window (they sit last).
    displaced: list[SearchHit] = []
    for h in reversed(window):
        if len(displaced) >= len(promote):
            break
        if not _is_chunk(h):
            displaced.append(h)
    keep_ids = {id(h) for h in displaced}
    new_window = [h for h in window if id(h) not in keep_ids] + promote
    return new_window + list(reversed(displaced)) + kept_rest


def cap_chunk_share(hits: list[SearchHit], top_k: int, max_share: float) -> list[SearchHit]:
    """Keep the sorted order, but let verbatim-chunk hits take
    at most `max_share` of the first `top_k` slots while non-chunk hits
    remain to fill them; the displaced chunks follow after the window so a
    larger `top_k` or a page still reaches them. `max_share >= 1.0` is a
    no-op; a window with no other hits stays all chunks — except at zero:
    `max_share <= 0` is the caller saying "facts only" (a namespace whose
    values are one-line facts),
    and a fact-only read that hands back one chunk id because nothing else
    matched is the defect it was set to avoid. Zero drops chunks outright and
    may return fewer than `top_k` rows."""
    if top_k <= 0 or not hits:
        return hits
    if max_share <= 0.0:
        return [
            h
            for h in hits
            if not (
                (getattr(h, "source", "") or "") == "chunk"
                or str(h.memory_key).startswith("chunk:")
            )
        ]
    if max_share >= 1.0:
        return hits
    allowed = int(top_k * max_share)
    window: list[SearchHit] = []
    deferred: list[SearchHit] = []
    rest_iter = iter(hits)
    chunks_in = 0
    for h in rest_iter:
        if len(window) >= top_k:
            deferred.append(h)
            continue
        is_chunk = (getattr(h, "source", "") or "") == "chunk" or str(h.memory_key).startswith(
            "chunk:"
        )
        if is_chunk and chunks_in >= allowed:
            deferred.append(h)
            continue
        if is_chunk:
            chunks_in += 1
        window.append(h)
    if len(window) < top_k and deferred:
        # Not enough non-chunk hits to fill the window: chunks fill the rest.
        need = top_k - len(window)
        window.extend(deferred[:need])
        deferred = deferred[need:]
    return window + deferred


def chunk_share(hits: list[Any], top_k: int) -> float:
    """Share of the first `top_k` hits that are verbatim chunks (ledger
    reading `out_chunk_share`)."""
    win = list(hits)[: max(1, int(top_k))]
    if not win:
        return 0.0
    n = sum(
        1
        for h in win
        if (getattr(h, "source", "") or "") == "chunk"
        or str(getattr(h, "memory_key", "")).startswith("chunk:")
    )
    return round(n / len(win), 2)
