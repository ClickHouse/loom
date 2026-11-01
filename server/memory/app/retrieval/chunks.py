"""The verbatim-chunk leg over loom.memory_chunks: stage-1 candidates,
the parent and date joins, the one-round-trip stage 2, neighbour windows,
and the rank score of one chunk hit.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from server.core.vectors.search_settings import vector_search_settings
from server.memory.app.retrieval.retry import _call_retrying_empty_block
from server.memory.domain.passage_trim import trim_chunk_excerpt
from server.memory.domain.search_parsing import _iso_date
from server.memory.domain.search_types import (
    SearchHit,
    SearchQuery,
    _CHClient,
    hit_sort_key,
    row_identity,
)
from server.memory.domain.sensitivity import reads_high

log = logging.getLogger("server.memory.app.retrieval")


# Ordinal-decay entity-overlap boost for the top-K parents matching a query's
# entity filter (rank 0 → 0.40 … rank 4 → 0.04). Boosting by overlap *rank*
# rather than raw count keeps magnitudes bounded on entity-dense rows.
_ENTITY_RANK_BOOSTS = [0.40, 0.25, 0.15, 0.08, 0.04]


def _chunk_candidates_sql(agent_pred: str) -> str:
    """Stage 1 of the chunk leg: the one query shape the codes-first read
    serves — a bare `ORDER BY cosineDistance LIMIT` over the table with no
    FINAL and no JOIN — returning only the chunk identity, its parent key and
    its distance.

    The codec's shortlist over-fetch inherits the vector multiplier (3): 1
    changed the 180-candidate set on the largest tenant (Jaccard 0.636
    against 3; 3 against itself 1.000). No
    `max_threads` pin: on a no-FINAL scan the threads follow the marks
    and a pin of 4 cost 40% (106 -> 150-183 ms). The parent
    key rides along (lazily materialised for the LIMIT rows only) so stage 2
    can key its parent-date read instead of merging the namespace.
    """
    return f"""
        SELECT
            c.session_id AS session_id,
            c.chunk_index AS chunk_index,
            c.parent_memory_key AS parent_memory_key,
            cosineDistance(c.embedding, {{q:Array(Float32)}}) AS distance
        FROM loom.memory_chunks AS c
        WHERE c.org = {{org:String}}
          AND c.namespace = {{ns:String}}
          AND c.embedding_model = {{model:String}}
          AND c.embedding_dim > 0
          AND c.tombstoned_at IS NULL
          {agent_pred}
        ORDER BY distance ASC
        LIMIT {{chunk_limit:UInt32}}
        {vector_search_settings(table="loom.memory_chunks")}
    """


def _chunk_parent_pred(q: SearchQuery, params: dict[str, Any]) -> str:
    """The read's hard tag filter, `as_of` anchor and served-status set,
    carried into the chunk leg through the parent row.

    `memory_chunks` has no `tags`, `valid_at` or `status` column; the parent
    in `memory_embeddings` has all three. A search that names `tags` admits
    a chunk only when its parent passes the fact leg's own predicates —
    `tag_predicate`, `as_of_predicate`, `status_predicate`, the same helpers
    with the same parameter names, so the two legs cannot drift. A search
    that names only `as_of` excludes the chunks whose parent is known and
    was not valid then; a chunk whose parent is a session (no embeddings
    row) keeps its session date and stays. Without either filter the leg is
    unchanged. Returns "" when nothing applies; binds its parameters into
    `params` (the chunk statement's, both stages read them)."""
    from server.core.tag_sql import tag_predicate
    from server.memory.domain.search_sql import hard_tag_filter
    from server.memory.domain.temporal_sql import (
        as_of_predicate,
        status_predicate,
        tombstone_predicate,
    )

    hard_tags = hard_tag_filter(q.tags, q.tags_soft)
    replaying = q.as_of is not None
    if not hard_tags and not replaying:
        return ""
    identity = ["org = {org:String}", "namespace = {ns:String}"]
    if q.scope == "private" and q.agent not in ("*", "-"):
        identity.append("agent = {agent:String}")
        params["agent"] = q.agent
    if replaying:
        params["as_of"] = q.as_of
    if hard_tags:
        params["tags"] = hard_tags
        where = [
            *identity,
            tombstone_predicate(q.as_of_basis, replaying=replaying),
            status_predicate(
                q.as_of_basis,
                replaying=replaying,
                include_duplicates=q.include_duplicates,
                include_superseded=q.include_superseded,
                include_proposed=q.include_proposed,
            ),
            tag_predicate(""),
        ]
        if replaying:
            where.append(
                as_of_predicate(q.as_of_basis, invalid_col="invalid_at", tomb_col="tombstoned_at")
            )
        return (
            "AND c.parent_memory_key IN (SELECT memory_key FROM loom.memory_embeddings FINAL"
            " WHERE " + " AND ".join(where) + ")"
        )
    # `as_of` alone: the parents that exist and fail the anchor are excluded;
    # an orphan chunk (session parent) is dated by its session, not here.
    not_valid = "NOT " + as_of_predicate(
        q.as_of_basis, invalid_col="invalid_at", tomb_col="tombstoned_at"
    )
    return (
        "AND c.parent_memory_key NOT IN (SELECT memory_key FROM loom.memory_embeddings FINAL"
        " WHERE " + " AND ".join([*identity, not_valid]) + ")"
    )


# The joined subqueries expose their keys under names the chunk query never
# uses bare. The chunk statements filter on `session_id` / `chunk_index`; once
# a joined relation carries a column of the same name, the analyzer (26.5 and
# 26.8 alike) rejects the bare identifier as ambiguous (code 207), and every
# neighbor-window read would fail that way and fall open to no neighbors.
# Belt and braces: the joined keys are prefixed AND the chunk
# predicates are table-qualified.
# The parent-date side is one row per memory_key. `memory_embeddings` is keyed
# by (agent, user, session, key), so a key that recurs across sessions has
# many rows (hundreds for one key on a large tenant), and a bare join would
# multiply every chunk by its parent's row count (72.9x over 180 candidates):
# the stage-2 LIMIT of 90 rows then carries 31 distinct chunks.
_CHUNK_PARENT_DATE_JOIN = """
        LEFT JOIN (
            SELECT memory_key AS pd_memory_key,
                   max(temporal_anchor) AS temporal_anchor,
                   max(valid_at) AS valid_at
            FROM loom.memory_embeddings FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND tombstoned_at IS NULL
            GROUP BY memory_key
        ) AS pd ON pd.pd_memory_key = c.parent_memory_key"""

_CHUNK_SESSION_DATE_JOIN = """
        LEFT JOIN (
            SELECT session_id AS ss_session_id, max(valid_at) AS session_date
            FROM loom.session_summaries FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND valid_at IS NOT NULL
            GROUP BY session_id
        ) AS ss ON ss.ss_session_id = c.session_id"""


# The same two joins keyed by what stage 1 already knows. The
# parent side is one row per memory_key and the digest side one row per
# session_id, so a filter on the group key changes nothing about the max()
# each group returns — it only stops the read from merging every row of the
# namespace under FINAL (52k `memory_embeddings` rows for a 3-chunk window on
# the largest tenant). The candidates' parent keys come from stage 1; the
# neighbour rows' parents are not known before the read, so they come from a
# narrow primary-key read of `memory_chunks` over the neighbour windows (no
# FINAL: a superset of keys is harmless, the join is by key).
_CHUNK_PARENT_DATE_JOIN_KEYED = """
        LEFT JOIN (
            SELECT memory_key AS pd_memory_key,
                   max(temporal_anchor) AS temporal_anchor,
                   max(valid_at) AS valid_at
            FROM loom.memory_embeddings FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND memory_key IN {pd_keys:Array(String)}
            GROUP BY memory_key
        ) AS pd ON pd.pd_memory_key = c.parent_memory_key"""

_CHUNK_PARENT_DATE_JOIN_KEYED_NEIGHBORS = """
        LEFT JOIN (
            SELECT memory_key AS pd_memory_key,
                   max(temporal_anchor) AS temporal_anchor,
                   max(valid_at) AS valid_at
            FROM loom.memory_embeddings FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND (memory_key IN {pd_keys:Array(String)}
                   OR memory_key IN (
                       SELECT parent_memory_key
                       FROM loom.memory_chunks
                       WHERE org = {org:String}
                         AND namespace = {ns:String}
                         AND (session_id, chunk_index) IN
                             arrayZip({nb_sess:Array(String)}, {nb_idx:Array(UInt32)})))
            GROUP BY memory_key
        ) AS pd ON pd.pd_memory_key = c.parent_memory_key"""

_CHUNK_SESSION_DATE_JOIN_KEYED = """
        LEFT JOIN (
            SELECT session_id AS ss_session_id, max(valid_at) AS session_date
            FROM loom.session_summaries FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND valid_at IS NOT NULL
              AND session_id IN {all_sess:Array(String)}
            GROUP BY session_id
        ) AS ss ON ss.ss_session_id = c.session_id"""


def _chunk_date_sql(
    source: str | None = None, *, keyed: bool = False, neighbors: bool = False
) -> tuple[str, str]:
    """SELECT expressions and JOIN clauses that date a verbatim chunk.

    `session` (default): the conversation date — the session digest's
    `valid_at`, which the ingest set from `observation_date` — with the parent
    fact's event anchor as the fallback for sessions that have no digest.
    `parent`: the parent fact's event anchor only.
    Both return the epoch sentinel for undated rows; `_iso_date` maps it to
    ''. The clauses use the `org` / `ns` query parameters every chunk query
    already binds.
    """
    if source is None:
        from server.settings import settings as _s

        source = str(_s.memory_search_chunk_date_source)
    if keyed:
        pd_join = (
            _CHUNK_PARENT_DATE_JOIN_KEYED_NEIGHBORS if neighbors else _CHUNK_PARENT_DATE_JOIN_KEYED
        )
        ss_join = _CHUNK_SESSION_DATE_JOIN_KEYED
    else:
        pd_join = _CHUNK_PARENT_DATE_JOIN
        ss_join = _CHUNK_SESSION_DATE_JOIN
    if source == "parent":
        select = (
            "toString(coalesce(pd.temporal_anchor, toDateTime(0))) AS temporal_anchor,\n"
            "            toString(coalesce(pd.valid_at, toDateTime(0))) AS valid_at"
        )
        return select, pd_join
    select = (
        "toString(coalesce(ss.session_date, pd.temporal_anchor, toDateTime(0)))"
        " AS temporal_anchor,\n"
        "            toString(coalesce(ss.session_date, pd.valid_at, toDateTime(0)))"
        " AS valid_at"
    )
    return select, ss_join + pd_join


def _chunk_query_tokens(s: str) -> set[str]:
    """Lowercased alpha-only tokens of length >= 3 (skips stopword-grade
    tokens); the chunk leg's density signal and its "does the query carry
    words at all" test for neighbour expansion."""
    out: set[str] = set()
    cur: list[str] = []
    for ch_ in s.lower():
        if ch_.isalpha():
            cur.append(ch_)
        else:
            if len(cur) >= 3:
                out.add("".join(cur))
            cur = []
    if len(cur) >= 3:
        out.add("".join(cur))
    return out


# How many of stage 1's best distinct chunks get their ±1
# window read in the same statement as the re-read. The anchors are the top
# three rows stage 2 returns; stage 2 keeps stage 1's order, so its top three
# are the first three of stage 1's distinct identities that survive FINAL.
# Reading windows for six covers up to three collapsed or tombstoned
# versions ahead of them; an anchor outside the six falls back to the
# separate neighbour statement (correct, one round trip slower).
_CHUNK_ANCHOR_PREFETCH = 6


def _chunk_anchor_prefetch(candidates: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """The first `_CHUNK_ANCHOR_PREFETCH` distinct (session, index) pairs of
    stage 1 in stage 2's order (distance, session, index)."""
    best: dict[tuple[str, int], float] = {}
    for r in candidates:
        key = (str(r["session_id"]), int(r["chunk_index"]))
        d = float(r.get("distance") or 0.0)
        if key not in best or d < best[key]:
            best[key] = d
    ordered = sorted(best.items(), key=lambda kv: (kv[1], kv[0][0], kv[0][1]))
    return [k for k, _d in ordered[:_CHUNK_ANCHOR_PREFETCH]]


def _chunk_window_idents(anchors: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """(session, idx-1..idx+1) for every anchor, deduplicated, index >= 0."""
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for sess, idx in anchors:
        for i in range(max(0, idx - 1), idx + 2):
            if (sess, i) not in seen:
                seen.add((sess, i))
                out.append((sess, i))
    return out


def _chunk_stage2_one_trip_sql(
    *,
    chunk_agent_pred: str,
    overlap_join: str,
    overlap_count_select: str,
    distance_select: str,
    date_select: str,
    date_join: str,
    neighbors: bool,
) -> str:
    """Stage 2 of the chunk leg in one round trip.

    Two primary-key reads of `memory_chunks FINAL` in one statement: the
    candidate re-read (stage 1's identities, distance looked up by identity,
    the entity-overlap join and the LIMIT) and, when the query carries words, the
    ±1 windows of stage 1's best chunks with their exact `cosineDistance` —
    the 6 KB vector is read for those few rows only, never for the
    candidates' granules. The date joins sit outside the union so both
    branches are dated the same way; they are keyed (see
    `_CHUNK_PARENT_DATE_JOIN_KEYED`) so nothing merges the namespace. Plain
    WHERE under FINAL; `use_skip_indexes = 0` because both reads are
    primary-key lookups.

    `optimize_move_to_prewhere_if_final = 1` lets the planner move the
    sorting-key predicates (org, namespace, the identity IN lists,
    `memory_key IN`, `session_id IN`) ahead of the wide columns; ClickHouse
    moves nothing else under FINAL (`embedding_model`, `tombstoned_at`,
    `embedding_dim` stay in WHERE, checked by EXPLAIN), and every version of
    a row shares its key, so the FINAL collapse sees the same rows. Without
    it a FINAL read materialises the whole granule: the window branch read
    55 MB of `embedding` for 18 chunks, 5.5 MB with it; the statement 66 →
    17 MB. The fact leg's identity PREWHERE is the same move written by hand.
    """
    identity = f"""
              WHERE c.org = {{org:String}}
                AND c.namespace = {{ns:String}}
                AND c.embedding_model = {{model:String}}
                AND c.embedding_dim > 0
                AND c.tombstoned_at IS NULL
                {chunk_agent_pred}"""
    cand_branch = f"""
            (SELECT
                c.agent AS agent,
                c.user_id AS user_id,
                c.session_id AS session_id,
                c.chunk_index AS chunk_index,
                c.content_sha256 AS content_sha256,
                c.created_at AS created_at,
                c.parent_memory_key AS parent_memory_key,
                c.content AS content,
                {distance_select} AS distance,
                {overlap_count_select} AS entity_overlap_count,
                0 AS is_neighbor
            FROM loom.memory_chunks AS c FINAL
            {overlap_join}
            {identity}
                AND (c.session_id, c.chunk_index) IN arrayZip({{cand_sess:Array(String)}}, {{cand_idx:Array(UInt32)}})
            ORDER BY distance ASC, c.session_id ASC, c.chunk_index ASC,
                     c.created_at DESC, c.content_sha256 ASC
            LIMIT {{chunk_limit:UInt32}})"""
    nb_branch = ""
    if neighbors:
        nb_branch = f"""
            UNION ALL
            (SELECT
                c.agent AS agent,
                c.user_id AS user_id,
                c.session_id AS session_id,
                c.chunk_index AS chunk_index,
                c.content_sha256 AS content_sha256,
                c.created_at AS created_at,
                c.parent_memory_key AS parent_memory_key,
                c.content AS content,
                cosineDistance(c.embedding, {{q:Array(Float32)}}) AS distance,
                0 AS entity_overlap_count,
                1 AS is_neighbor
            FROM loom.memory_chunks AS c FINAL
            {identity}
                AND (c.session_id, c.chunk_index) IN arrayZip({{nb_sess:Array(String)}}, {{nb_idx:Array(UInt32)}}))"""
    return f"""
        SELECT
            concat('chunk:', toString(c.session_id),
                   ':', toString(c.chunk_index)) AS memory_key,
            'episodic' AS memory_type,
            substring(c.content, 1, 1200) AS content_excerpt,
            c.content AS content_full,
            c.agent AS agent,
            c.user_id AS user_id,
            c.session_id AS session_id,
            c.chunk_index AS chunk_index,
            c.content_sha256 AS content_sha256,
            c.created_at AS created_at,
            c.parent_memory_key AS parent_memory_key,
            c.distance AS distance,
            c.entity_overlap_count AS entity_overlap_count,
            c.is_neighbor AS is_neighbor,
            {date_select}
        FROM (
            {cand_branch}
            {nb_branch}
        ) AS c
        {date_join}
        SETTINGS use_skip_indexes = 0, optimize_move_to_prewhere_if_final = 1
    """


def _chunk_row_sha(r: dict[str, Any]) -> str:
    v = r.get("content_sha256")
    if isinstance(v, bytes | bytearray):
        return bytes(v).decode("ascii", "replace")
    return str(v or "")


def _chunk_row_age(r: dict[str, Any]) -> float:
    """Negative creation time, so a sort ascending puts the newest first;
    0.0 for a row without one (the stub clients)."""
    v = r.get("created_at")
    if v is None or v == "":
        return 0.0
    if hasattr(v, "timestamp"):
        return -float(v.timestamp())
    try:
        return -float(v)
    except (TypeError, ValueError):
        return 0.0


def _chunk_stage2_order(r: dict[str, Any]) -> tuple[float, str, int, float, str]:
    """The candidate branch's order (`ORDER BY distance, session_id,
    chunk_index`), applied in Python to the union's candidate rows so the
    anchor choice and the parent-boost ranking see the rows in that order —
    completed with the newest version first (`created_at DESC`, the table's
    own ReplacingMergeTree rule) and the row's `content_sha256`, the last
    column of the sorting key. Two stored versions of one chunk (same
    identity, different content) share distance, session and index; without
    the tie-breakers their order, and which of them falls on the LIMIT
    boundary, is the sort's plan-dependent order. The same total order as
    `hit_sort_key`, applied to the version.
    """
    return (
        float(r.get("distance") or 0.0),
        str(r.get("session_id") or ""),
        int(r.get("chunk_index") or 0),
        _chunk_row_age(r),
        _chunk_row_sha(r),
    )


async def _chunk_query(ch: _CHClient, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """One chunk-leg statement under the fresh-part retry. The leg catches
    its own errors to fail open, so the retry has to run inside it: only a
    code 10 that outlives the retries, or any other error, degrades the leg."""
    return await _call_retrying_empty_block(lambda: ch.query_all(sql, params=params), leg="chunk")


def _chunk_identity_pred(q: SearchQuery, params: dict[str, Any]) -> str:
    """The chunk leg's identity terms (both stages carry them), binding their
    parameters into `params`. Per-agent and per-user scoping mirror
    `_build_where` ('*'/'-' = deliberate cross-agent read; no user = every
    user); run scope reads a run's own passages. The session bound of the read (an own_session read scope, or
    the caller's session pick) holds here as on the fact leg; session_id
    leads the chunk table's sort key after the namespace, so it also prunes
    the scan."""
    terms: list[str] = []
    if q.scope == "private" and q.agent not in ("*", "-"):
        terms.append("AND c.agent = {agent:String}")
        params["agent"] = q.agent
    if q.run_id is not None:
        terms.append("AND c.run_id = {run_id:String}")
        params["run_id"] = q.run_id
    # A user-bounded read (read_scope own_user, a pinned token, a user filter)
    # reads only chunks stamped with that user; chunks stamped '-' are excluded
    # (fail closed).
    if q.user_id is not None:
        terms.append("AND c.user_id = {user_id:String}")
        params["user_id"] = q.user_id
    if q.session_id is not None:
        terms.append("AND c.session_id = {session_id:String}")
        params["session_id"] = q.session_id
    if q.session_ids:
        terms.append("AND c.session_id IN {session_ids:Array(String)}")
        params["session_ids"] = [str(x) for x in q.session_ids]
    return "\n          ".join(terms)


def _chunk_overlap_join(q: SearchQuery, chunk_params: dict[str, Any]) -> tuple[str, str]:
    """The entity-overlap LEFT JOIN and the count it selects, or ('', '0') with
    no entity filter.

    entity_overlap_count MUST come from a non-correlated LEFT JOIN keyed on
    memory_key: a correlated scalar subquery raises Code 48 NOT_IMPLEMENTED
    (CH can't decorrelate it). Parent rows are FINAL-collapsed pre-JOIN so
    each parent contributes one count."""
    if not q.entity_filter:
        return "", "0"
    chunk_params["ent_filter"] = [t.lower() for t in q.entity_filter]
    overlap_join = """
            LEFT JOIN (
                SELECT memory_key,
                       length(arrayIntersect(entities,
                                             {ent_filter:Array(String)}))
                           AS entity_overlap_count
                FROM loom.memory_embeddings FINAL
                WHERE org = {org:String}
                  AND namespace = {ns:String}
                  AND tombstoned_at IS NULL
            ) AS eo ON eo.memory_key = c.parent_memory_key
        """
    return overlap_join, "ifNull(eo.entity_overlap_count, 0)"


def _chunk_parent_boosts(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Rank parents with entity overlap by count desc; assign boosts by rank."""
    parents_by_count: dict[str, int] = {}
    for r in rows:
        pk = r.get("parent_memory_key") or ""
        cnt = int(r.get("entity_overlap_count") or 0)
        if cnt > 0 and pk:
            parents_by_count[pk] = max(parents_by_count.get(pk, 0), cnt)
    ranked_parents = sorted(parents_by_count.items(), key=lambda kv: -kv[1])
    parent_boost: dict[str, float] = {}
    for i, (pk, _cnt) in enumerate(ranked_parents):
        if i >= len(_ENTITY_RANK_BOOSTS):
            break
        parent_boost[pk] = _ENTITY_RANK_BOOSTS[i]
    return parent_boost


def _chunk_primary_hit(
    r: dict[str, Any],
    q: SearchQuery,
    q_tokens: set[str],
    q_tokens_all: set[str],
    *,
    boost: float,
    parity: bool,
    alpha: float,
) -> SearchHit:
    """One stage-2 chunk row as a hit: cosine, the parent's entity boost, a
    capped token-density bonus and (under hybrid parity) a lexical score."""
    # Token-density bonus, capped so it can't outweigh cosine.
    token_overlap = len(q_tokens & _chunk_query_tokens(r.get("content_full") or ""))
    density_bonus = min(0.10, 0.02 * token_overlap)
    distance = float(r["distance"])
    _lex = 0.0
    if parity and q_tokens_all:
        _full = (r.get("content_full") or "").lower()
        _lex = sum(1 for t in q_tokens_all if t in _full) / float(len(q_tokens_all))
    rank_score = chunk_rank_score(
        distance, boost, density_bonus, lexical=_lex, alpha=alpha, parity=parity
    )
    # Trim the excerpt to the query-relevant turn(s); ranking already
    # scored content_full above, so this is display-only.
    return SearchHit(
        memory_key=r["memory_key"],
        memory_type=str(r["memory_type"]),
        content_excerpt=trim_chunk_excerpt(r["content_excerpt"], q.query_text or ""),
        distance=distance,
        rank_score=rank_score,
        temporal_anchor=_iso_date(str(r.get("temporal_anchor", ""))),
        temporal_confidence=float(r.get("temporal_confidence") or 0.0),
        valid_at=_iso_date(str(r.get("valid_at", ""))),
        source="chunk",
        parent_memory_key=str(r.get("parent_memory_key") or ""),
        session_id=str(r.get("session_id") or ""),
        identity=row_identity(r),
        created_at=str(r.get("created_at") or ""),
        score_components={
            "semantic": max(0.0, 1.0 - distance),
            "chunk_parent_boost": float(boost),
            "token_density": float(density_bonus),
            "rank_score": float(rank_score),
        },
    )


def _chunk_anchor_keys(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """The (session, index) of the top-3 anchors whose ±1 neighbours are pulled
    in. The same anchor can surface more than once (one row per parent fact);
    one window per distinct (session, index) — a repeated OR arm is a
    repeated scan for the same rows."""
    anchor_keys: list[tuple[str, int]] = []
    for r in rows[:3]:
        sess = str(r.get("session_id") or "")
        idx = int(r.get("chunk_index") or 0)
        if sess and (sess, idx) not in anchor_keys:
            anchor_keys.append((sess, idx))
    return anchor_keys


async def _chunk_anchor_windows(
    ch: _CHClient,
    anchor_keys: list[tuple[str, int]],
    *,
    prefetched: list[tuple[str, int]],
    window_rows: list[dict[str, Any]],
    chunk_agent_pred: str,
    chunk_params: dict[str, Any],
) -> list[dict[str, Any]]:
    """The neighbour rows of the chosen anchors. The windows of stage 1's best
    chunks came back with the re-read; an anchor the prefetch did not cover
    (more than three of stage 1's best collapsed under FINAL) reads its window
    in a separate statement."""
    covered = [a for a in anchor_keys if a in prefetched]
    fallback_anchors = [a for a in anchor_keys if a not in prefetched]
    wanted = set(_chunk_window_idents(covered))
    neighbor_rows = [
        r
        for r in window_rows
        if (str(r.get("session_id") or ""), int(r.get("chunk_index") or 0)) in wanted
    ]
    if fallback_anchors:
        neighbor_sql, neighbor_params = _chunk_neighbor_sql(
            fallback_anchors, chunk_agent_pred=chunk_agent_pred, base_params=chunk_params
        )
        try:
            neighbor_rows.extend(await _chunk_query(ch, neighbor_sql, neighbor_params))
        except Exception as e:  # noqa: BLE001 — neighbour chunks are optional context around the hits
            log.warning("chunk leg: neighbour window read failed (served without): %s", e)
    return neighbor_rows


def _chunk_neighbor_hit(r: dict[str, Any], q: SearchQuery, q_tokens: set[str]) -> SearchHit:
    """A neighbour chunk as a hit: cosine + density only (no entity boost), so
    neighbours rank below the anchors they were pulled in by."""
    overlap = len(q_tokens & _chunk_query_tokens(r.get("content_full") or ""))
    density_bonus = min(0.10, 0.02 * overlap)
    distance = float(r["distance"])
    rank_score = (1.0 - distance) + density_bonus
    return SearchHit(
        memory_key=r["memory_key"],
        memory_type=str(r["memory_type"]),
        content_excerpt=trim_chunk_excerpt(r["content_excerpt"], q.query_text or ""),
        distance=distance,
        rank_score=rank_score,
        temporal_anchor=_iso_date(str(r.get("temporal_anchor", ""))),
        temporal_confidence=float(r.get("temporal_confidence") or 0.0),
        valid_at=_iso_date(str(r.get("valid_at", ""))),
        source="chunk",
        parent_memory_key=str(r.get("parent_memory_key") or ""),
        session_id=str(r.get("session_id") or ""),
        identity=row_identity(r),
        created_at=str(r.get("created_at") or ""),
        score_components={
            "semantic": max(0.0, 1.0 - distance),
            "token_density": float(density_bonus),
            "rank_score": float(rank_score),
        },
    )


async def _search_memory_chunks(
    ch: _CHClient,
    q: SearchQuery,
    parent_params: dict[str, Any],
) -> list[SearchHit]:
    """Cosine + rank-decayed entity-overlap boost + neighbor expansion.

    The verbatim-chunks layer is a parallel retrieval source: extracted facts
    come from memory_embeddings, raw chunks from here. Three signals stack:
      1. Cosine to query (base rank).
      2. Rank-decayed entity-overlap boost (parents ranked by overlap count,
         magnitudes from _ENTITY_RANK_BOOSTS; rank 5+ gets nothing).
      3. Neighbor expansion: top anchors pull their chunk_index ±1 neighbors
         so multi-chunk passages stay together, re-ranked by token density.

    Returns hits with memory_key prefix 'chunk:{session}:{idx}'. Empty on
    empty table, or on a CH error that outlives the fresh-part retry.
    """
    chunk_params = {
        "q": parent_params["q"],
        "org": q.org,
        "ns": q.namespace,
        "model": q.embedding_model,
        # Over-fetch for anchors + neighbor-expansion headroom.
        "chunk_limit": min(200, int(q.top_k) * 3),
    }
    # Identity terms here; the read's tag filter, `as_of` and served statuses
    # are carried through the parent row.
    chunk_agent_pred = _chunk_identity_pred(q, chunk_params)
    if parent_pred := _chunk_parent_pred(q, chunk_params):
        chunk_agent_pred = f"{chunk_agent_pred}\n          {parent_pred}"
    overlap_join, overlap_count_select = _chunk_overlap_join(q, chunk_params)

    # Two stages. Stage 1 is the shape the codes-first read accepts
    # (bare ORDER BY cosineDistance LIMIT, no FINAL, no JOIN) and returns only
    # chunk identities; stage 2 is the full query below restricted to those
    # identities — a primary-key lookup under FINAL with skip indexes off, so
    # every version of a candidate is seen and collapses properly. A single
    # query that joined the parent dates between the vector read and the LIMIT
    # could not take the codes-first read: a full-precision scan of every
    # chunk in the namespace on every search.

    q_tokens = _chunk_query_tokens(q.query_text or "")
    stage1_params = {
        **chunk_params,
        # Over-fetch so versions that collapse in stage 2 leave >= n rows.
        "chunk_limit": int(chunk_params["chunk_limit"]) * 2,
    }
    try:
        stage1_sql = _chunk_candidates_sql(chunk_agent_pred)
        candidates = await _chunk_query(ch, stage1_sql, stage1_params)
    except Exception:
        log.exception(
            "chunk plane query failed; degrading to fact-only retrieval "
            "(namespace=%s entity_filter=%s stage=candidates)",
            q.namespace,
            bool(q.entity_filter),
        )
        return []
    # Only rows carrying a chunk identity are candidates; anything else
    # (a stub, a malformed row) is ignored rather than raised on.
    candidates = [
        r
        for r in candidates
        if r.get("session_id") not in (None, "") and r.get("chunk_index") is not None
    ]
    if not candidates:
        return []
    chunk_params["cand_sess"] = [str(r["session_id"]) for r in candidates]
    chunk_params["cand_idx"] = [int(r["chunk_index"]) for r in candidates]
    # Stage 2 never touches `c.embedding` for a candidate. Stage 1 already
    # paid for every candidate's distance; stage 2 looks it up by chunk
    # identity instead of re-reading a 6 KB vector per row of every
    # granule the candidates land in (568 MB → the content column only on
    # a 100k-chunk namespace). 1.0 is the fallback for a row the lookup
    # misses, which the identity IN makes impossible.
    chunk_params["cand_ident"] = [
        f"{r['session_id']}\x1f{int(r['chunk_index'])}" for r in candidates
    ]
    chunk_params["cand_dist"] = [float(r.get("distance") or 0.0) for r in candidates]
    distance_select = (
        "transform(concat(toString(c.session_id), '\\x1f', toString(c.chunk_index)), "
        "{cand_ident:Array(String)}, {cand_dist:Array(Float32)}, 1.0)"
    )

    # Stage 2 is one round trip: the candidate re-read, the ±1 windows of
    # stage 1's best chunks and the keyed date reads ride in one statement.
    # A chunk is dated by the conversation it was said in:
    # the session digest's valid_at is the observation date the ingest was
    # given, so the reader resolves "yesterday" against the right day. The
    # parent fact's event anchor is the fallback (older ingests with no
    # digest); orphan chunks (no digest, no parent) get NULL → empty.
    # Neighbour expansion only happens for a query that carries words
    # (the density signal needs them); without them the union has one
    # branch and the date reads are keyed by the candidates alone.
    neighbors = bool(q_tokens)
    windows: list[tuple[str, int]] = []
    prefetched: list[tuple[str, int]] = []
    if neighbors:
        prefetched = _chunk_anchor_prefetch(candidates)
        windows = _chunk_window_idents(prefetched)
        chunk_params["nb_sess"] = [sess for sess, _i in windows]
        chunk_params["nb_idx"] = [i for _sess, i in windows]
    chunk_params["pd_keys"] = sorted({str(r.get("parent_memory_key") or "") for r in candidates})
    chunk_params["all_sess"] = sorted(
        set(chunk_params["cand_sess"]) | {sess for sess, _i in windows}
    )
    date_select, date_join = _chunk_date_sql(keyed=True, neighbors=neighbors)
    sql = _chunk_stage2_one_trip_sql(
        chunk_agent_pred=chunk_agent_pred,
        overlap_join=overlap_join,
        overlap_count_select=overlap_count_select,
        distance_select=distance_select,
        date_select=date_select,
        date_join=date_join,
        neighbors=neighbors,
    )
    try:
        rows = await _chunk_query(ch, sql, chunk_params)
    except Exception:
        # Degrade to fact-only retrieval rather than 500'ing the query, but
        # log it — a silently-disabled chunk plane must leave a trace.
        log.exception(
            "chunk plane query failed; degrading to fact-only retrieval "
            "(namespace=%s entity_filter=%s)",
            q.namespace,
            bool(q.entity_filter),
        )
        return []
    # The union comes back unordered; the candidate rows take their
    # distance order, the window rows wait for the anchors below.
    window_rows = sorted(
        (r for r in rows if int(r.get("is_neighbor") or 0)), key=_chunk_stage2_order
    )
    rows = sorted((r for r in rows if not int(r.get("is_neighbor") or 0)), key=_chunk_stage2_order)
    if not rows:
        return []

    parent_boost = _chunk_parent_boosts(rows)

    _parity = q.search_mode in ("lexical", "hybrid", "bm25_hybrid", "rrf")
    # Digit runs (PR/issue/test numbers) count as tokens for
    # the parity score: the alpha-only tokenizer above drops them, so a chunk
    # that cites "107585" would get no credit while the fact leg pins the number.
    q_tokens_all = q_tokens | set(re.findall(r"\d{4,}", q.query_text or ""))
    _alpha = float(q.alpha or 0.5)

    primary_hits = [
        _chunk_primary_hit(
            r,
            q,
            q_tokens,
            q_tokens_all,
            boost=parent_boost.get(r.get("parent_memory_key") or "", 0.0),
            parity=_parity,
            alpha=_alpha,
        )
        for r in rows
    ]
    primary_hits.sort(key=hit_sort_key)

    # Neighbor expansion: for the top-3 anchors, fetch ±1 chunk_index
    # neighbors from the same session, ranked below their anchor.
    if not primary_hits or not q_tokens:
        return _served(primary_hits, rows, int(q.top_k))

    anchor_keys = _chunk_anchor_keys(rows)
    if not anchor_keys:
        return _served(primary_hits, rows, int(q.top_k))
    neighbor_rows = await _chunk_anchor_windows(
        ch,
        anchor_keys,
        prefetched=prefetched,
        window_rows=window_rows,
        chunk_agent_pred=chunk_agent_pred,
        chunk_params=chunk_params,
    )
    seen_keys = {h.memory_key for h in primary_hits}
    for r in neighbor_rows:
        mk = r["memory_key"]
        if mk in seen_keys:
            continue
        seen_keys.add(mk)
        primary_hits.append(_chunk_neighbor_hit(r, q, q_tokens))

    primary_hits.sort(key=hit_sort_key)
    return _served(primary_hits, [*rows, *neighbor_rows], int(q.top_k) * 3)


def _served(hits: list[SearchHit], rows: list[dict[str, Any]], n: int) -> list[SearchHit]:
    """The first `n` hits whose chunk text does not read as high. A chunk written
    as one has no vector and never gets here; this holds back one an earlier build embedded.
    Every embedded version of a key the read returned is checked: a hit is held back when
    any of them reads as high, whichever one the hit came from."""
    versions: dict[str, list[str]] = {}
    for r in rows:
        versions.setdefault(str(r["memory_key"]), []).append(str(r["content_full"]))
    served: list[SearchHit] = []
    for hit in hits:
        if len(served) >= n:
            break
        if not any(reads_high(text) for text in versions[hit.memory_key]):
            served.append(hit)
    return served


def _chunk_neighbor_sql(
    anchor_keys: list[tuple[str, int]],
    *,
    chunk_agent_pred: str,
    base_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """The separate ±1 neighbour-window statement: a third round trip, only
    for an anchor the stage-2 prefetch did not cover. Neighbours are dated
    the same way as their anchors and scoped like them (the leg's identity and
    parent predicates)."""
    date_select, date_join = _chunk_date_sql()
    neighbor_predicates = []
    neighbor_params: dict[str, Any] = {
        "q": base_params["q"],
        "org": base_params["org"],
        "ns": base_params["ns"],
        "model": base_params["model"],
    }
    # Every parameter the leg's identity and parent predicates name (agent, run_id, user,
    # session, session set, tags, as_of) — bound from the stage statements' own params.
    for k in set(re.findall(r"\{(\w+):", chunk_agent_pred)):
        neighbor_params[k] = base_params[k]
    for i, (sess, idx) in enumerate(anchor_keys):
        ns_key = f"ns{i}"
        idx_lo = f"lo{i}"
        idx_hi = f"hi{i}"
        neighbor_params[ns_key] = sess
        neighbor_params[idx_lo] = max(0, idx - 1)
        neighbor_params[idx_hi] = idx + 1
        # Qualified: `c.` keeps the predicate unambiguous next to the date
        # joins, which also expose session_id.
        neighbor_predicates.append(
            f"(c.session_id = {{{ns_key}:String}} "
            f" AND c.chunk_index BETWEEN {{{idx_lo}:UInt32}} "
            f" AND {{{idx_hi}:UInt32}})"
        )
    neighbor_sql = f"""
            SELECT
                concat('chunk:', toString(c.session_id),
                       ':', toString(c.chunk_index)) AS memory_key,
                'episodic' AS memory_type,
                substring(c.content, 1, 1200) AS content_excerpt,
                c.content AS content_full,
                cosineDistance(c.embedding, {{q:Array(Float32)}}) AS distance,
                c.agent AS agent,
                c.user_id AS user_id,
                c.session_id AS session_id,
                c.parent_memory_key AS parent_memory_key,
                c.created_at AS created_at,
                {date_select}
            FROM loom.memory_chunks AS c FINAL
            {date_join}
            WHERE c.org = {{org:String}}
              AND c.namespace = {{ns:String}}
              AND c.embedding_model = {{model:String}}
              AND c.embedding_dim > 0
              AND c.tombstoned_at IS NULL
              {chunk_agent_pred}
              AND ({" OR ".join(neighbor_predicates)})
        """
    return neighbor_sql, neighbor_params


def chunk_rank_score(
    distance: float,
    parent_boost: float,
    density_bonus: float,
    *,
    lexical: float = 0.0,
    alpha: float = 0.5,
    parity: bool = False,
) -> float:
    """Rank of one verbatim-chunk hit. The default shape is cosine + parent
    boost + token density. With `parity` the
    lexical modes score a chunk the way they score a fact row —
    alpha * lexical + (1 - alpha) * cosine — so a chunk that carries the
    query's identifier competes with the fact rows that carry it, instead of
    every hybrid window being facts only."""
    cosine = 1.0 - distance
    base = alpha * lexical + (1.0 - alpha) * cosine if parity else cosine
    return base + parent_boost + density_bonus
