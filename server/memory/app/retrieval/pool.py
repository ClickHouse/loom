"""The fused pool's last steps: folding memory.set chunks onto their parent
fact, the tag-scoped recency backfill and the recency re-rank, and the
chunk-share cap and floor on the window.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, NamedTuple

from server.core.ch_params import fitting
from server.core.repo_base import query_all_sliced
from server.memory.app.retrieval.side_legs import exclude_pooled
from server.memory.app.set_chunks import set_chunk_identities
from server.memory.domain.search_parsing import _iso_date
from server.memory.domain.search_sql import _build_where
from server.memory.domain.search_types import (
    RowIdentity,
    RowKey,
    SearchHit,
    SearchQuery,
    _CHClient,
    hit_sort_key,
    row_identity,
    row_key_of,
)
from server.memory.store.rows import parse_memory_value
from server.memory.store.shared import SHARED_AGENT
from server.settings import settings

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
    alone. Rows already in the pool are excluded, up to one form field of them; the new
    rows enter at the pool's lowest rank score — in scope, so as good as the weakest candidate
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
            e.version AS version,
            e.tags AS tags
        FROM loom.memory_embeddings AS e FINAL
        WHERE {" AND ".join(where)}
        -- rows that tie are taken in row order
        ORDER BY coalesce(e.valid_at, e.temporal_anchor, e.created_at) DESC,
                 e.memory_key ASC, e.agent, e.user_id, e.session_id
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
                version=int(r.get("version") or 0),
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


def _folded_fields(row: SearchHit, chunk: SearchHit) -> dict[str, Any]:
    """The fields a row takes when a chunk of its value folds onto it: one more folded
    chunk and, when the chunk out-scored it, the chunk's score, distance and passage."""
    components = dict(row.score_components or {})
    components["folded_chunks"] = float(components.get("folded_chunks", 0.0)) + 1.0
    if float(chunk.rank_score or 0.0) <= float(row.rank_score or 0.0):
        return {"score_components": components}
    components["folded_lift"] = 1.0
    return {
        "score_components": components,
        "rank_score": float(chunk.rank_score),
        "distance": min(float(row.distance), float(chunk.distance)),
        "content_excerpt": chunk.content_excerpt or row.content_excerpt,
    }


class SetChunkRow(NamedTuple):
    """The row a memory.set chunk was cut from, its vector's version and tags."""

    identity: RowIdentity
    version: int
    tags: tuple[str, ...]


# the key's promoted row: tags unknown, so alone it fails a hard tag filter
_SHARED_SET_CHUNK_ROW = SetChunkRow((SHARED_AGENT, "-", "-"), 0, ())


class ChunkId(NamedTuple):
    """A pooled set chunk: its writer, its parent key, and its own index and hash."""

    agent: str
    user_id: str
    memory_key: str
    chunk_index: int
    sha: str


def fold_set_chunks(hits: list[SearchHit], rows: Mapping[ChunkId, SetChunkRow]) -> list[SearchHit]:
    """Under private scope, fold the verbatim chunks of long memory.set values onto the rows
    `rows` names (`_set_chunk_rows`): onto the row when it is pooled, as its hit when it is
    not. A chunk `rows` names no row for is dropped: no served row's value cuts it."""

    def named(h: SearchHit) -> SetChunkRow | None:
        cid = _chunk_id(h)
        return rows.get(cid) if cid is not None else None

    return _fold(hits, named, _row_place)


def _row_place(who: RowIdentity | None, key: str) -> tuple[str, ...]:
    # a pooled fact that names no row keys as `(key,)`, which no named row matches
    return (key,) if who is None else (*who, key)


def fold_shared_set_chunks(hits: list[SearchHit]) -> list[SearchHit]:
    """Under shared scope, where the writer's private row is never served, a chunk folds onto
    a pooled fact of its key, or becomes the hit of the key's promoted row."""
    return _fold(hits, lambda h: _SHARED_SET_CHUNK_ROW, lambda who, key: (key,))


def _fold(
    hits: list[SearchHit],
    named: Callable[[SearchHit], SetChunkRow | None],
    place: Callable[[RowIdentity | None, str], tuple[str, ...]],
) -> list[SearchHit]:
    """Fold each set chunk onto the pooled hit of the row `named` gives it, or add it as
    that row's hit (a chunk `named` gives none is dropped). `place` keys a row, pooled or
    named, by its identity and key, so a chunk meets its pooled row."""
    if not hits or not any(_is_set_chunk(h) for h in hits):
        return hits
    out = [h for h in hits if not _is_set_chunk(h)]
    at: dict[tuple[str, ...], int] = {}
    for pos, h in enumerate(out):
        if h.source != "chunk":
            at.setdefault(place(h.identity, h.memory_key), pos)
    for h in hits:
        if not _is_set_chunk(h) or (row := named(h)) is None:
            continue
        target = place(row.identity, str(h.parent_memory_key))
        if target in at:
            parent = out[at[target]]
            out[at[target]] = replace(parent, **_folded_fields(parent, h))
            continue
        index = _chunk_index(h)
        _, _, session = row.identity
        converted = replace(
            h,
            memory_key=str(h.parent_memory_key),
            source="fact",
            parent_memory_key="",
            identity=row.identity,
            session_id=session,
            version=row.version,
            tags=list(row.tags),
            score_components={
                **dict(h.score_components or {}),
                "folded_chunks": 1.0,
                "folded_from_chunk": float(index) if index is not None else -1.0,
                # the score is the chunk's: no fact-only boost may be added
                # downstream (asserted tie-break) — the chunk never had it
                "folded_lift": 1.0,
            },
        )
        at[target] = len(out)
        out.append(converted)
    out.sort(key=hit_sort_key)
    return out


def _chunk_index(h: SearchHit) -> int | None:
    # `chunk:memory.set:<key>:<index>`
    index = str(h.memory_key).rpartition(":")[2]
    return int(index) if index.isdigit() else None


def _chunk_id(h: SearchHit) -> ChunkId | None:
    index = _chunk_index(h)
    if h.identity is None or index is None:
        return None
    return ChunkId(h.identity[0], h.identity[1], str(h.parent_memory_key), index, h.chunk_sha256)


async def _set_chunk_rows(
    ch: _CHClient, q: SearchQuery, hits: list[SearchHit]
) -> dict[ChunkId, SetChunkRow]:
    """The served row each pooled set chunk was cut from: of the writer's served rows of the
    key with the newest vectors (`memory_search_set_chunk_rows` of them; a status change
    re-writes a vector too), the first whose current value cuts that chunk
    (`set_chunk_identities`). A chunk none of them cuts, or every chunk when a
    read fails, names none. Two writers whose values cut one chunk share its `memory_chunks`
    row, stamped with the last of them, so the chunk names that writer's memory row. Private
    scope only (`fold_shared_set_chunks` reads no row)."""
    pooled = {cid for h in hits if _is_set_chunk(h) and (cid := _chunk_id(h)) is not None}
    if not pooled:
        return {}
    # served whatever its vector: a row whose embedding is still pending has its chunks
    where, params = _build_where(q, ranked=False)
    # one form field: a chunk of a writer and key past it names no row and drops
    params["set_rows"] = fitting(sorted({(c.agent, c.user_id, c.memory_key) for c in pooled}))
    params["per_key"] = int(settings.memory_search_set_chunk_rows)
    try:
        served = await ch.query_all(
            f"""
            SELECT e.agent AS agent, e.user_id AS user_id, e.session_id AS session_id,
                   e.memory_key AS memory_key, max(e.version) AS version,
                   argMax(e.tags, e.version) AS tags
            FROM loom.memory_embeddings AS e FINAL
            WHERE e.org = {{org:String}}
              AND e.namespace = {{ns:String}}
              AND (e.agent, e.user_id, e.memory_key)
                  IN {{set_rows:Array(Tuple(String, String, String))}}
            GROUP BY e.agent, e.user_id, e.session_id, e.memory_key
            HAVING argMax({" AND ".join(where)}, e.version)
            ORDER BY version DESC, agent, user_id, session_id  -- ties keep one order
            LIMIT {{per_key:UInt32}} BY agent, user_id, memory_key
            """,
            params=params,
        )
        values = await _row_values(ch, q, served)
    except Exception as e:  # noqa: BLE001 — naming a chunk's row never fails a search
        log.warning("set chunk rows read failed; this pool's set chunks are dropped: %s", e)
        return {}
    by_writer: dict[tuple[str, str, str], list[ChunkId]] = {}
    for c in pooled:
        by_writer.setdefault((c.agent, c.user_id, c.memory_key), []).append(c)
    out: dict[ChunkId, SetChunkRow] = {}
    # rows of a key written in many sessions often hold one value: chunk it once
    cuts_of: dict[tuple[str, str], frozenset[tuple[int, str]]] = {}
    for r in served:  # newest first: the first row whose value cuts a chunk names it
        row = row_key_of(q.namespace, r)
        if row not in values:
            continue
        text, memory_type = values[row]
        if (text, memory_type) not in cuts_of:
            cuts_of[text, memory_type] = set_chunk_identities(parse_memory_value(text), memory_type)
        cuts = cuts_of[text, memory_type]
        for c in by_writer.get((row.agent, row.user_id, row.memory_key), []):
            if (c.chunk_index, c.sha) in cuts:
                identity = (row.agent, row.user_id, row.session_id)
                out.setdefault(c, SetChunkRow(identity, int(r["version"]), tuple(r["tags"])))
    return out


async def _row_values(
    ch: _CHClient, q: SearchQuery, rows: list[dict[str, Any]]
) -> dict[RowKey, tuple[str, str]]:
    """The current stored value text and memory_type of each live row, read by
    `loom.memory`'s sort-key prefix a form field at a time."""
    sql = """
        SELECT agent, user_id, session_id, memory_key, memory_type, value
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (agent, user_id, session_id, memory_key)
              IN {rows:Array(Tuple(String, String, String, String))}
          AND tombstoned_at IS NULL
    """
    keys = sorted({(r["agent"], r["user_id"], r["session_id"], r["memory_key"]) for r in rows})
    found = await query_all_sliced(ch, sql, {"org": q.org, "ns": q.namespace, "rows": keys}, "rows")
    return {row_key_of(q.namespace, r): (str(r["value"]), str(r["memory_type"])) for r in found}


async def _finish_pool(ch: _CHClient, hits: list[SearchHit], q: SearchQuery) -> list[SearchHit]:
    """The fused pool's last steps, once per namespace: the newest rows in a
    tag-scoped recency read join the pool (`_recency_backfill`), the recency
    pool re-rank when the query carries a recency cue and
    the namespace (or request) set a weight — the SQL term 0.25/(1+age_years)
    tips ties between days, not months — then the chunk share cap and floor
    and the window cut. No LLM rerank here: it runs
    once, in the handler, on the cross-namespace pool."""
    if q.scope == "private":
        hits = fold_set_chunks(hits, await _set_chunk_rows(ch, q, hits))
    else:
        hits = fold_shared_set_chunks(hits)
    hits = await _recency_backfill(ch, q, hits)
    if q.recency_intent and float(q.recency_pool_weight) > 0.0:
        hits = rerank_by_recency_pool(hits, weight=float(q.recency_pool_weight))
    # The request's (or namespace's) chunk share cap; unset = no cap.
    share = 1.0 if q.chunk_max_share is None else float(q.chunk_max_share)
    hits = cap_chunk_share(hits, int(q.top_k), share)
    hits = floor_chunk_share(
        hits,
        int(q.top_k),
        effective_chunk_floor(float(settings.memory_search_chunk_min_share), share),
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
