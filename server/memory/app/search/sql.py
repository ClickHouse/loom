"""Statement builders for the additive pool arms (anchor pool, key match, digest
descent): each arm's identity predicates and its statement text."""

from __future__ import annotations

from typing import Any

from server.core.tag_sql import tag_predicate
from server.memory.app.search_tag_filter import request_hard_tags as _request_hard_tags
from server.memory.app.served_superseded import served_flags as _served
from server.memory.domain.search_sql import agent_filter, agent_or_shared_predicate
from server.memory.domain.temporal_sql import as_of_predicate, served_status_sql


def _read_scope_where(input: Any, params: dict[str, Any], alias: str = "e") -> list[str]:
    """The read-scope and replay predicates the fact leg applies (`memory/domain/search_sql`):
    the caller's user, session or session set, and the as-of cut. Without them an additive
    arm would serve rows of another user or session to a `read_scope=user|session` read, or
    rows written after an as-of replay's instant."""
    col = f"{alias}." if alias else ""
    parts: list[str] = []
    user_id = getattr(input, "user_id", None)
    if user_id is not None:
        parts.append(f"{col}user_id = {{rs_user_id:String}}")
        params["rs_user_id"] = user_id
    session_id = getattr(input, "session_id", None)
    if session_id is not None:
        parts.append(f"{col}session_id = {{rs_session_id:String}}")
        params["rs_session_id"] = session_id
    session_ids = [str(s) for s in (getattr(input, "session_ids", None) or [])]
    if session_ids:
        parts.append(f"{col}session_id IN {{rs_session_ids:Array(String)}}")
        params["rs_session_ids"] = session_ids
    as_of = getattr(input, "as_of", None)
    if as_of is not None:
        parts.append(
            as_of_predicate(
                getattr(input, "as_of_basis", "valid_time"),
                alias=alias,
                param="rs_as_of",
                invalid_col="invalid_at",
                tomb_col="tombstoned_at",
            )
        )
        params["rs_as_of"] = as_of
    return parts


def _live_window_where(input: Any, alias: str = "e") -> list[str]:
    """The fact leg's liveness window (`memory/domain/search_sql._build_where`): an expired row
    is never served, and a row whose stated validity has ended (`valid_to`) is not current;
    under an as-of replay the as-of predicate owns the validity window instead. Without them
    an additive arm would serve a fact the dense leg already stopped serving."""
    col = f"{alias}." if alias else ""
    parts = [f"({col}expires_at IS NULL OR {col}expires_at > now())"]
    if getattr(input, "as_of", None) is None:
        parts.append(f"({col}valid_to IS NULL OR {col}valid_to > now())")
    return parts


def _anchor_pool_where(input: Any, params: dict[str, Any]) -> list[str]:
    """Identity predicates of the entity/temporal anchor pool: the fact leg's identity, the
    query's embedding model, the served statuses, the agent (own rows plus shared), the run and
    the request's hard tag filter; the caller binds `model` and adds the anchor terms.

    Without them the pool would read past what every other leg serves: another run's trace on
    the strength of the entities it carried, a superseded version or an unapproved proposal,
    or a cosine against another model's vectors."""
    parts = [
        "e.org = {org:String}",
        "e.namespace = {ns:String}",
        "e.embedding_model = {model:String}",
        "e.tombstoned_at IS NULL",
        "e.embedding_dim > 0",
        served_status_sql(alias="e", **_served(input)),
    ]
    agent = agent_filter(getattr(input, "agent", ""))
    if agent:
        parts.append(agent_or_shared_predicate("e"))
        params["agent"] = agent
    run_id = str(getattr(input, "run_id", "") or "")
    if run_id:
        parts.append("e.run_id = {run_id:String}")
        params["run_id"] = run_id
    # The hard tag filter the fact leg carries, on this leg too.
    hard_tags = _request_hard_tags(input)
    if hard_tags:
        parts.append(tag_predicate("e"))
        params["tags"] = hard_tags
    return parts + _live_window_where(input) + _read_scope_where(input, params)


def anchor_pool_sql(where_sql: str) -> str:
    """The entity / temporal anchor pool's statement: tags selected, plus the row's identity
    and scope so the post-fusion belt can judge the row in process. `where_sql` is
    `_anchor_pool_where` plus the anchor terms."""
    return f"""
                SELECT e.memory_key AS memory_key,
                       e.memory_type AS memory_type,
                       e.content_excerpt AS content_excerpt,
                       1.0 - cosineDistance(e.embedding, {{qv:Array(Float32)}}) AS similarity,
                       toString(coalesce(e.temporal_anchor, toDateTime(0))) AS temporal_anchor,
                       e.temporal_confidence AS temporal_confidence,
                       toString(coalesce(e.valid_at, toDateTime(0))) AS valid_at,
                       e.session_id AS session_id,
                       ifNull(e.source_resource_id, '') AS source_resource_id,
                       e.tags AS tags,
                       e.agent AS agent,
                       e.user_id AS user_id,
                       e.scope AS scope
                FROM loom.memory_embeddings AS e FINAL
                WHERE {where_sql}
                ORDER BY cosineDistance(e.embedding, {{qv:Array(Float32)}}) ASC
                LIMIT {{limit:UInt32}}
            """


def key_match_precheck_sql() -> str:
    """Can any key in the namespace satisfy the arm's stem predicate? Keys only — no FINAL,
    no vector column, no per-stem weights — stopping at the first row. `(matched >= 2 OR
    (matched >= 1 AND matched = key_parts))` is the wide statement's own qualification
    computed from `memory_key` alone, and every version of a row carries the same key, so an
    empty answer here means the wide statement (`key_match_sql`) would return nothing under any
    version rule; a row here says nothing about the strength filter, and the wide statement
    runs as usual."""
    return """
        SELECT 1 AS any_key
        FROM (
            SELECT arrayCount(t -> has({stems:Array(String)}, t), key_stems) AS matched,
                   length(key_stems) AS key_parts
            FROM (
                SELECT arrayMap(t -> stem(t, 'en'),
                                splitByChar('_', lower(memory_key))) AS key_stems
                FROM loom.memory_embeddings
                WHERE org = {org:String} AND namespace = {ns:String}
            )
        )
        WHERE matched >= 2 OR (matched >= 1 AND matched = key_parts)
        LIMIT 1
    """


def key_match_sql(where_sql: str) -> str:
    """The key-match arm's statement.

    Three passes over the namespace's keys: `n_keys` and the per-stem
    document frequencies are narrow FINAL scans (memory_key and the key
    columns only); the wide pass carries every column, the vectors included.
    The wide pass filters on the key in PREWHERE — a row whose key shares no stem with the query
    is dropped before its 6 KB of vectors are read. `memory_key` is in the table's ORDER BY, so
    the filter sees the same value on every version of a row and is safe under FINAL. Without
    it, a mid-size namespace read 239 MB and 110k rows per call to return 0–5 rows.
    """
    return f"""
        WITH
            (SELECT toFloat64(count()) FROM loom.memory_embeddings FINAL
              WHERE org = {{org:String}} AND namespace = {{ns:String}}
                AND tombstoned_at IS NULL) AS n_keys,
            (SELECT mapFromArrays(groupArray(s), groupArray(toFloat64(c)))
               FROM (
                 SELECT s, count() AS c
                 FROM (
                   SELECT arrayJoin(arrayDistinct(arrayMap(t -> stem(t, 'en'),
                          splitByChar('_', lower(memory_key))))) AS s
                   FROM loom.memory_embeddings FINAL
                   WHERE org = {{org:String}} AND namespace = {{ns:String}}
                     AND tombstoned_at IS NULL
                 )
                 WHERE has({{stems:Array(String)}}, s)
                 GROUP BY s
               )) AS df,
            arraySum(arrayMap(q -> log(1.0 + assumeNotNull(n_keys)
                                            / greatest(1.0, assumeNotNull(df)[q])),
                              {{stems:Array(String)}})) AS q_weight
        SELECT e.memory_key AS memory_key,
               e.memory_type AS memory_type,
               e.content_excerpt AS content_excerpt,
               1.0 - cosineDistance(e.embedding, {{qv:Array(Float32)}}) AS similarity,
               toString(coalesce(e.temporal_anchor, toDateTime(0))) AS temporal_anchor,
               e.temporal_confidence AS temporal_confidence,
               toString(coalesce(e.valid_at, toDateTime(0))) AS valid_at,
               e.agent AS agent,
               e.user_id AS user_id,
               e.session_id AS session_id,
               e.version AS version,
               ifNull(e.source_resource_id, '') AS source_resource_id,
               toString(e.created_at) AS created_at,
               e.tags AS tags,
               matched, key_parts,
               arraySum(arrayMap(q -> log(1.0 + assumeNotNull(n_keys)
                                               / greatest(1.0, assumeNotNull(df)[q])),
                        arrayFilter(k -> has({{stems:Array(String)}}, k),
                                    arrayDistinct(key_stems)))) / q_weight AS strength
        FROM (
            SELECT *,
                   arrayMap(t -> stem(t, 'en'),
                            splitByChar('_', lower(memory_key))) AS key_stems,
                   arrayCount(t -> has({{stems:Array(String)}}, t), key_stems) AS matched,
                   length(key_stems) AS key_parts
            FROM loom.memory_embeddings FINAL
            PREWHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND arrayExists(t -> has({{stems:Array(String)}}, stem(t, 'en')),
                              splitByChar('_', lower(memory_key)))
            
        ) AS e
        WHERE {where_sql}
          AND (matched >= 2 OR (matched >= 1 AND matched = key_parts))
          AND strength >= {{min_strength:Float64}}
        ORDER BY strength DESC, cosineDistance(e.embedding, {{qv:Array(Float32)}}) ASC
        LIMIT {{per_key:UInt32}} BY e.memory_key
        LIMIT {{limit:UInt32}}
    """


def key_match_component_sql(where_sql: str) -> str:
    """The key-match arm's component-only statement: the strength of the keys already in the
    pool, and nothing else. The same idf weights as `key_match_sql` — `n_keys` and the per-stem
    document frequencies over the namespace's live keys, here folded into one narrow pass
    (`sumMap` over each row's distinct matching stems; measured identical to the two-pass form,
    163 → 130 ms server, 14 → 10 MiB) — and the same predicate and floor, but the candidate
    read is restricted to `{pool_keys}` and touches no vector or text column: the wide FINAL
    pass that costs 289 of the full statement's 304 MiB on a 30k-row namespace is not issued.
    Used when the gate says only a pooled row can be affected (`pool_key`), so a row the pool
    missed would not have been served.
    """
    return f"""
        WITH
            (SELECT (toFloat64(count()),
                     sumMap(arrayFilter(s -> has({{stems:Array(String)}}, s),
                                        arrayDistinct(arrayMap(
                                            t -> stem(t, 'en'),
                                            splitByChar('_', lower(memory_key))))) AS ks,
                            arrayResize([toUInt64(1)], length(ks), toUInt64(1))))
               FROM loom.memory_embeddings FINAL
              WHERE org = {{org:String}} AND namespace = {{ns:String}}
                AND tombstoned_at IS NULL) AS stats,
            assumeNotNull(tupleElement(assumeNotNull(stats), 1)) AS n_keys,
            mapFromArrays(tupleElement(tupleElement(assumeNotNull(stats), 2), 1),
                          arrayMap(c -> toFloat64(c),
                                   tupleElement(tupleElement(assumeNotNull(stats), 2), 2))) AS df,
            arraySum(arrayMap(q -> log(1.0 + n_keys / greatest(1.0, df[q])),
                              {{stems:Array(String)}})) AS q_weight
        SELECT e.memory_key AS memory_key,
               arraySum(arrayMap(q -> log(1.0 + n_keys / greatest(1.0, df[q])),
                        arrayFilter(k -> has({{stems:Array(String)}}, k),
                                    arrayDistinct(key_stems)))) / q_weight AS strength
        FROM (
            SELECT memory_key, org, namespace, agent, scope, run_id, embedding_model,
                   embedding_dim, tombstoned_at, status, tags,
                   -- read-scope and as-of columns (`_read_scope_where`)
                   user_id, session_id, created_at, valid_at, temporal_anchor, invalid_at,
                   -- liveness window (`_live_window_where`)
                   expires_at, valid_to,
                   arrayMap(t -> stem(t, 'en'),
                            splitByChar('_', lower(memory_key))) AS key_stems,
                   arrayCount(t -> has({{stems:Array(String)}}, t), key_stems) AS matched,
                   length(key_stems) AS key_parts
            FROM loom.memory_embeddings FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_key IN {{pool_keys:Array(String)}}
              
        ) AS e
        WHERE {where_sql}
          AND (matched >= 2 OR (matched >= 1 AND matched = key_parts))
          AND strength >= {{min_strength:Float64}}
        ORDER BY strength DESC, e.memory_key
        LIMIT 1 BY e.memory_key
        LIMIT {{limit:UInt32}}
    """


def _key_match_where(input: Any, params: dict[str, Any]) -> list[str]:
    """The key-match arm's identity predicates: the fact leg's identity, the served statuses,
    the agent (own rows plus shared), the run and the request's hard tag filter."""
    where = [
        "e.org = {org:String}",
        "e.namespace = {ns:String}",
        "e.embedding_model = {model:String}",
        "e.embedding_dim > 0",
        "e.tombstoned_at IS NULL",
        served_status_sql(alias="e", **_served(input)),
    ]
    agent = agent_filter(getattr(input, "agent", ""))
    if agent:
        where.append(agent_or_shared_predicate("e"))
        params["agent"] = agent
    if str(getattr(input, "run_id", "") or ""):
        # without it the key-match arm would read past the run
        where.append("e.run_id = {run_id:String}")
        params["run_id"] = str(input.run_id)
    hard_tags = _request_hard_tags(input)
    if hard_tags:
        where.append(tag_predicate("e"))
        params["tags"] = hard_tags
    return where + _live_window_where(input) + _read_scope_where(input, params)


def _digest_descent_where(input: Any, params: dict[str, Any]) -> list[str]:
    """The descent's predicates over one session's facts: identity, the served statuses, the
    agent (own plus shared), the run and the request's hard tag filter. `params` gains the
    bound values it adds."""
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "session_id = {sid:String}",
        "embedding_model = {model:String}",
        "embedding_dim > 0",
        "tombstoned_at IS NULL",
        served_status_sql(**_served(input)),
        # A raw tool observation (`obs:`) is the run ledger's row, not a fact the session
        # learned; the descent never serves one.
        "NOT startsWith(memory_key, 'obs:')",
    ]
    agent = agent_filter(getattr(input, "agent", ""))
    if agent:
        where.append(agent_or_shared_predicate(""))
        params["agent"] = agent
    if str(getattr(input, "run_id", "") or ""):
        # a session's facts can belong to several runs; under a run the
        # descent reads only its own
        where.append("run_id = {run_id:String}")
        params["run_id"] = str(input.run_id)
    hard_tags = _request_hard_tags(input)
    if hard_tags:
        where.append(tag_predicate(""))
        params["tags"] = hard_tags
    return where + _live_window_where(input, alias="") + _read_scope_where(input, params, alias="")


def digest_descent_sql(where_sql: str) -> str:
    """The descent's statement: one session's facts nearest the query, one row per writer and
    key, tags selected."""
    return f"""
                SELECT memory_key, memory_type, content_excerpt,
                       1.0 - cosineDistance(embedding, {{qv:Array(Float32)}}) AS similarity,
                       toString(coalesce(temporal_anchor, toDateTime(0))) AS temporal_anchor,
                       temporal_confidence,
                       toString(coalesce(valid_at, toDateTime(0))) AS valid_at,
                       ifNull(source_resource_id, '') AS source_resource_id,
                       toString(created_at) AS created_at,
                       tags, agent, user_id, session_id, version
                FROM loom.memory_embeddings FINAL
                WHERE {where_sql}
                ORDER BY cosineDistance(embedding, {{qv:Array(Float32)}}) ASC
                LIMIT 1 BY agent, user_id, memory_key
                LIMIT {{k:UInt32}}
            """
