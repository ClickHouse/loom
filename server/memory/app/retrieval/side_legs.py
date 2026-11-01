"""The legs that run beside the fact leg and add to its pool: the lexical
recall pool, the records leg over loom.memory_records, and the
`include_top_n_unmatched` candidates primary recall missed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from server.memory.app.retrieval.retry import _call_retrying_empty_block
from server.memory.domain.search_parsing import _iso_date, _record_excerpt, _record_query_tokens
from server.memory.domain.search_sql import _build_where, exclude_pooled
from server.memory.domain.search_text import _GENERIC_ENTITY_FILTER
from server.memory.domain.search_types import SearchHit, SearchQuery, _CHClient, row_identity

log = logging.getLogger("server.memory.app.retrieval")


async def _lexical_recall_fallback(
    ch: _CHClient,
    q: SearchQuery,
    *,
    limit: int = 50,
    max_distance: float = 0.9,
) -> list[SearchHit]:
    """BM25-only candidate pool — catches term-rich rows the vector leg missed.

    Pulls candidates from the lexical index even when their embedding sits
    outside the vector leg's nearest rows. Wins on terminology-heavy
    workloads (API names, error codes) the embedder under-weights.

    `max_distance` is the semantic guard: a lexically-matched row whose cosine
    distance exceeds it is a pure-lexical false friend (shared token, different
    meaning) and is dropped. Set high (e.g. 2.0) to admit every lexical hit.
    """
    if not q.query_text or not q.query_text.strip():
        return []
    # Alphanumeric, min length 2, lowercased.
    tokens: list[str] = []
    cur: list[str] = []
    for ch_ in q.query_text.lower():
        if ch_.isalnum():
            cur.append(ch_)
        else:
            if len(cur) >= 2:
                tokens.append("".join(cur))
            cur = []
    if len(cur) >= 2:
        tokens.append("".join(cur))
    # Dedup + cap to avoid pathological queries.
    tokens = list(dict.fromkeys(tokens))[:32]
    tokens = [t for t in tokens if t not in _GENERIC_ENTITY_FILTER]
    # A compound the corpus knows is a token here too — it
    # is the only way the pool meets an identifier the query spells in words.
    tokens.extend(c for c in q.lexical_compounds if c not in tokens)
    if not tokens:
        return []

    from server.memory.domain.lemmatize import lemmagen_lemma_tokens as _lemmagen
    from server.memory.domain.lemmatize import snowball_stem_tokens as _snowball

    # Reuse _build_where so the lexical pool honors EVERY caller filter the
    # vector pool does (tags, scope, status, TTL, ...). A bare WHERE here would
    # leak rows matching a token but not the caller's filter; the lexical
    # token match is the only clause this pool adds on top.
    where, params = _build_where(q)
    params["tokens"] = tokens
    params["limit"] = int(limit)
    params["stem_tokens"] = _snowball(tokens) or tokens
    params["lemma_tokens"] = _lemmagen(tokens) or tokens
    where.append(
        "(hasAnyTokens(e.content_excerpt_lc, {tokens:Array(String)}) "
        "OR hasAnyTokens(e.content_excerpt_stem, {stem_tokens:Array(String)}) "
        "OR hasAnyTokens(e.content_excerpt_lemma, {lemma_tokens:Array(String)}))"
    )
    where_sql = " AND ".join(where)
    # Lex score: count of query tokens present in content_excerpt_lc.
    lex_expr = (
        "arraySum(arrayMap(t -> if("
        "positionCaseInsensitive(e.content_excerpt_lc, t) > 0, 1.0, 0.0"
        "), {tokens:Array(String)}))"
    )
    sql = f"""
        SELECT
            e.memory_key       AS memory_key,
            e.memory_type      AS memory_type,
            e.content_excerpt  AS content_excerpt,
            e.status           AS status,
            e.version          AS version,
            cosineDistance(e.embedding, {{q:Array(Float32)}}) AS distance,
            {lex_expr} AS lex_score,
            toString(coalesce(e.temporal_anchor, toDateTime(0))) AS temporal_anchor,
            e.temporal_confidence AS temporal_confidence,
            toString(coalesce(e.valid_at, toDateTime(0))) AS valid_at,
            ifNull(e.source_resource_id, '') AS source_resource_id,
            e.source_class AS source_class,
            e.verified AS verified,
            e.agent AS agent,
            e.user_id AS user_id,
            e.session_id AS session_id,
            e.tags AS tags
        FROM loom.memory_embeddings AS e FINAL
        WHERE {where_sql}
        ORDER BY lex_score DESC, distance ASC, e.memory_key ASC
        LIMIT {{limit:UInt32}}
        SETTINGS query_plan_direct_read_from_text_index = 0
    """
    try:
        rows = await _call_retrying_empty_block(
            lambda: ch.query_all(sql, params=params), leg="lexical"
        )
    except Exception as e:  # noqa: BLE001 — the lexical fallback leg is additive; fail open
        log.warning("_lexical_recall_fallback failed (fail-open): %s", e)
        return []

    out: list[SearchHit] = []
    n_tokens = max(1, len(tokens))
    for r in rows:
        mk = r["memory_key"]
        # Normalize lex to [0,1] by token count, same scale as cosine.
        lex_norm = float(r.get("lex_score", 0) or 0) / n_tokens
        distance = float(r["distance"])
        # Drop lexical hits that are also semantically far (false friends).
        if distance > max_distance:
            continue
        rank_score = lex_norm + 0.3 * max(0.0, 1.0 - distance)
        out.append(
            SearchHit(
                memory_key=mk,
                memory_type=str(r["memory_type"]),
                content_excerpt=r["content_excerpt"],
                distance=distance,
                rank_score=rank_score,
                temporal_anchor=_iso_date(str(r.get("temporal_anchor", ""))),
                temporal_confidence=float(r.get("temporal_confidence") or 0.0),
                valid_at=_iso_date(str(r.get("valid_at", ""))),
                source="fact",
                source_resource_id=str(r.get("source_resource_id") or ""),
                source_class=str(r.get("source_class") or ""),
                verified=bool(int(r.get("verified") or 0)),
                session_id=str(r.get("session_id") or ""),
                identity=row_identity(r),
                version=int(r.get("version") or 0),
                # The row's tags ride so the post-fusion belt
                # can check the hard filter without a second read.
                tags=[str(t) for t in (r.get("tags") or []) if t],
                score_components={
                    "lexical": float(lex_norm),
                    "semantic": 0.3 * max(0.0, 1.0 - distance),
                    "rank_score": float(rank_score),
                },
            )
        )
        if len(out) >= limit:
            break
    return out


def _record_identity_where(q: SearchQuery, params: dict[str, Any]) -> list[str]:
    """The records leg's identity and validity terms, binding their parameters into
    `params`: agent (a record written without one stays readable), user, the read's
    session bound (an own_session read scope, the caller's session pick — as on the
    fact, chunk and digest legs) and the `as_of` window."""
    where: list[str] = []
    if q.scope == "private" and q.agent not in ("*", "-"):
        where.append("r.identity_agent IN {record_agents:Array(String)}")
        params["record_agents"] = [q.agent, ""]
    if q.user_id is not None:
        where.append("r.user_id = {user_id:String}")
        params["user_id"] = q.user_id
    if q.session_id is not None:
        where.append("r.session_id = {session_id:String}")
        params["session_id"] = q.session_id
    if q.session_ids:
        where.append("r.session_id IN {session_ids:Array(String)}")
        params["session_ids"] = [str(x) for x in q.session_ids]
    if q.as_of is not None:
        where.append(
            "(r.valid_at IS NULL OR r.valid_at <= {as_of:DateTime}) "
            "AND (r.valid_to IS NULL OR r.valid_to >= {as_of:DateTime})"
        )
        params["as_of"] = q.as_of
    return where


async def _search_memory_records(
    ch: _CHClient,
    q: SearchQuery,
    *,
    limit: int = 25,
) -> list[SearchHit]:
    """Additive retrieval over schema-grounded memory_records.

    This is the executable "record" plane that the planner already emits for
    exact-state, temporal, needle, and aggregation-shaped queries. It is a
    lexical route over structured rows, not a replacement for vector facts.
    If a request uses a hard filter that records cannot currently honor
    (tags, category, run_id, memory_type, or shared scope), skip records
    rather than leaking mismatched results.
    """

    if (
        limit <= 0
        or q.scope != "private"
        or q.memory_type is not None
        or q.run_id is not None
        or q.tags
        or q.category
    ):
        return []

    tokens = _record_query_tokens(q)
    if not tokens:
        return []

    where = [
        "r.org = {org:String}",
        "r.namespace = {ns:String}",
        "r.status = 'active'",
        "r.tombstoned_at IS NULL",
    ]
    params: dict[str, Any] = {
        "org": q.org,
        "ns": q.namespace,
        "tokens": tokens,
        "limit": int(limit),
    }
    where += _record_identity_where(q, params)

    recency_expr = "0.0"
    if q.recency_intent:
        recency_expr = (
            "0.15 / (1.0 + toFloat32(dateDiff('day', "
            "coalesce(valid_at, updated_at), now())) / 365.0)"
        )

    temporal_expr = "0.0"
    if q.query_anchor_iso:
        params["record_anchor"] = q.query_anchor_iso
        temporal_expr = (
            "if(valid_at IS NOT NULL AND abs(dateDiff('day', valid_at, "
            "parseDateTimeBestEffortOrNull({record_anchor:String}))) <= 14, "
            "0.20, 0.0)"
        )

    where_sql = " AND ".join(where)
    sql = f"""
        SELECT
            record_id,
            parent_memory_key,
            session_id,
            schema_name,
            record_json,
            field_text,
            lexical_score,
            least(
                1.0,
                0.80 * lexical_score + {recency_expr} + {temporal_expr}
            ) AS rank_score,
            -- NB: alias the stringified date as *_iso, NOT `valid_at`. A
            -- `toString(...) AS valid_at` here shadows the Nullable(DateTime)
            -- `valid_at` that recency_expr/temporal_expr feed to dateDiff() in
            -- this same SELECT — ClickHouse binds the String alias and raises
            -- code 43 ILLEGAL_TYPE_OF_ARGUMENT, silently failing the records
            -- leg open on every query carrying an anchor.
            toString(coalesce(valid_at, toDateTime(0))) AS valid_at_iso,
            toString(coalesce(valid_at, toDateTime(0))) AS temporal_anchor
        FROM
        (
            SELECT
                record_id,
                parent_memory_key,
                session_id,
                schema_name,
                record_json,
                field_text,
                valid_at,
                updated_at,
                haystack,
                arraySum(arrayMap(
                    t -> if(positionCaseInsensitive(haystack, t) > 0, 1.0, 0.0),
                    {{tokens:Array(String)}}
                )) / greatest(toFloat32(length({{tokens:Array(String)}})), 1.0)
                    AS lexical_score
            FROM
            (
                SELECT
                    toString(r.record_id) AS record_id,
                    ifNull(r.memory_key, '') AS parent_memory_key,
                    r.session_id AS session_id,
                    toString(r.schema_name) AS schema_name,
                    r.record_json AS record_json,
                    r.valid_at AS valid_at,
                    r.updated_at AS updated_at,
                    arrayStringConcat(
                        groupArrayIf(
                            concat(
                                ifNull(toString(f.field_name), ''),
                                ': ',
                                if(
                                    ifNull(toString(f.value_state), '') = 'value',
                                    ifNull(f.value_string, ''),
                                    ifNull(toString(f.value_state), '')
                                )
                            ),
                            f.record_id IS NOT NULL
                        ),
                        ' | '
                    ) AS field_text,
                    concat(
                        toString(r.schema_name), ' ',
                        ifNull(r.memory_key, ''), ' ',
                        r.record_json, ' ',
                        field_text
                    ) AS haystack
                FROM loom.memory_records AS r FINAL
                LEFT JOIN loom.memory_record_fields AS f FINAL
                  ON f.org = r.org
                 AND f.namespace = r.namespace
                 AND f.schema_name = r.schema_name
                 AND f.record_id = r.record_id
                 AND f.tombstoned_at IS NULL
                WHERE {where_sql}
                GROUP BY
                    r.record_id,
                    r.memory_key,
                    r.session_id,
                    r.schema_name,
                    r.record_json,
                    r.valid_at,
                    r.updated_at
            )
        )
        WHERE lexical_score > 0
        ORDER BY rank_score DESC, updated_at DESC, parent_memory_key ASC
        LIMIT {{limit:UInt32}}
    """
    try:
        rows = await _call_retrying_empty_block(
            lambda: ch.query_all(sql, params=params), leg="records"
        )
    except Exception as e:  # noqa: BLE001 — the records leg is additive; fail open
        log.warning("_search_memory_records failed (fail-open): %s", e)
        return []

    return _record_hits(rows)


def _record_hits(rows: list[dict[str, Any]]) -> list[SearchHit]:
    """The record leg's rows as hits, keyed by their parent memory when they have one."""
    out: list[SearchHit] = []
    for row in rows:
        record_id = str(row.get("record_id") or "")
        if not record_id:
            continue
        parent_key = str(row.get("parent_memory_key") or "")
        schema_name = str(row.get("schema_name") or "record")
        rank_score = float(row.get("rank_score") or 0.0)
        distance = max(0.0, min(1.0, 1.0 - rank_score))
        memory_key = parent_key or f"record:{schema_name}:{record_id}"
        out.append(
            SearchHit(
                memory_key=memory_key,
                memory_type="record",
                content_excerpt=_record_excerpt(
                    schema_name=schema_name,
                    memory_key=parent_key,
                    record_json=str(row.get("record_json") or "{}"),
                    field_text=str(row.get("field_text") or ""),
                ),
                distance=distance,
                rank_score=rank_score,
                temporal_anchor=_iso_date(str(row.get("temporal_anchor", ""))),
                valid_at=_iso_date(str(row.get("valid_at_iso", ""))),
                source="record",
                source_resource_id=record_id,
                session_id=str(row.get("session_id") or ""),
                # a record is no row of loom.memory: its writer's identity names none
                score_components={
                    "record_lexical": float(row.get("lexical_score") or 0.0),
                    "rank_score": rank_score,
                },
            )
        )
    return out


async def _fetch_unmatched_candidates(
    ch: _CHClient,
    q: SearchQuery,
    pooled: Sequence[SearchHit],
    *,
    limit: int,
) -> list[SearchHit]:
    """Fetch recency/confidence candidates that primary recall missed.

    This backs `include_top_n_unmatched`: a deliberately additive pool for
    downstream rerank/evidence packing. Rows are not required to clear lexical
    or vector relevance; they are bounded by the same identity filters and
    sorted by reinforcement/mention/creation signals.
    """

    if limit <= 0:
        return []

    where, params = _build_where(q)
    params["unmatched_limit"] = int(limit)
    exclude_pooled(where, params, pooled)

    where_sql = " WHERE " + " AND ".join(where)
    sql = f"""
        SELECT
            e.memory_key       AS memory_key,
            e.memory_type      AS memory_type,
            e.content_excerpt  AS content_excerpt,
            e.status           AS status,
            e.version          AS version,
            cosineDistance(e.embedding, {{q:Array(Float32)}}) AS distance,
            toString(coalesce(e.temporal_anchor, toDateTime(0))) AS temporal_anchor,
            e.temporal_confidence AS temporal_confidence,
            toString(coalesce(e.valid_at, toDateTime(0))) AS valid_at,
            ifNull(e.source_resource_id, '') AS source_resource_id,
            e.source_class AS source_class,
            e.verified AS verified,
            e.confidence AS confidence,
            e.reinforcement_count AS reinforcement_count,
            coalesce(e.mentioned_at, e.last_reinforced_at, e.created_at) AS freshness,
            e.agent AS agent,
            e.user_id AS user_id,
            e.session_id AS session_id,
            e.tags AS tags
        FROM loom.memory_embeddings AS e FINAL
        {where_sql}
        ORDER BY reinforcement_count DESC, confidence DESC, freshness DESC
        LIMIT {{unmatched_limit:UInt32}}
    """
    try:
        rows = await ch.query_all(sql, params=params)
    except Exception as e:  # noqa: BLE001 — the unmatched-candidate leg is additive; fail open
        log.warning("_fetch_unmatched_candidates failed (fail-open): %s", e)
        return []

    out: list[SearchHit] = []
    for r in rows:
        distance = float(r["distance"])
        confidence = float(r.get("confidence") or 0.0)
        reinforcement = float(r.get("reinforcement_count") or 0.0)
        # Keep below normal relevance scores; the reranker/packer can promote
        # genuinely useful rows, but stale popular rows should not dominate.
        rank_score = 0.05 * confidence + min(0.10, 0.01 * reinforcement)
        out.append(
            SearchHit(
                memory_key=str(r["memory_key"]),
                memory_type=str(r["memory_type"]),
                content_excerpt=str(r["content_excerpt"]),
                distance=distance,
                rank_score=rank_score,
                temporal_anchor=_iso_date(str(r.get("temporal_anchor", ""))),
                temporal_confidence=float(r.get("temporal_confidence") or 0.0),
                valid_at=_iso_date(str(r.get("valid_at", ""))),
                source="fact",
                source_resource_id=str(r.get("source_resource_id") or ""),
                source_class=str(r.get("source_class") or ""),
                verified=bool(int(r.get("verified") or 0)),
                session_id=str(r.get("session_id") or ""),
                identity=row_identity(r),
                version=int(r.get("version") or 0),
                tags=[str(t) for t in (r.get("tags") or []) if t],
                score_components={
                    "unmatched_confidence": 0.05 * confidence,
                    "unmatched_reinforcement": min(0.10, 0.01 * reinforcement),
                    "rank_score": float(rank_score),
                },
            )
        )
    return out
