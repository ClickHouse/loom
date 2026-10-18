"""The statements a conflict check reads its peer candidates with: the batched
join over a namespace's vectors, the two-stage nearest-vectors /
rows-by-sort-key pair, and the pinned-rows-first pair a narrowed writer adds."""

from __future__ import annotations

# The most peers a conflict check weighs per fact.
PEER_LIMIT = 10


def vector_param(i: int) -> str:
    """The placeholder for fact `i`'s vector (1-based). Each vector is its own
    parameter: ClickHouse caps one HTTP parameter at 128 KiB, which four 3072-d
    vectors in one array already exceed."""
    return f"{{emb{i}:Array(Float32)}}"


def vector_params(vectors: list[list[float]]) -> dict[str, list[float]]:
    """The values for `vector_param(1..n)`."""
    return {f"emb{i}": v for i, v in enumerate(vectors, start=1)}


_CANDIDATE_JOIN = """
        FROM loom.memory m FINAL
        JOIN loom.memory_embeddings e FINAL
          ON  e.org = m.org AND e.namespace = m.namespace
          AND e.memory_key = m.memory_key
          AND e.user_id = m.user_id AND e.session_id = m.session_id
          AND e.agent = m.agent"""


def candidate_sql_many(n: int, in_scope: str = "") -> str:
    """The batched form of `check_on_commit`'s peer statement:
    one read of the namespace's vectors serves `n` facts. Each fact's distance
    is its own `cosineDistance` term — an `arrayMap` over the vector column
    replicates the 6 KB vector once per fact per row (1.7 GB peak at n=12);
    the unrolled tuple array does not. `ARRAY JOIN` over the filtered tuples
    drops rows that match no fact, the per-fact `memory_type` / self-key
    predicates and distance ceiling ride in the filter, and `LIMIT ... BY qidx`
    is the per-fact statement's `LIMIT` (both `PEER_LIMIT`). Measured identical
    per-fact lists at n = 1, 4, 6, 8, 12, 24 against the per-fact statement. `in_scope`: the
    read-scope clause on `m`, so other users' rows never take the places
    (their pinned rows come from `pinned_sql_many`)."""
    if n < 1:
        raise ValueError("candidate_sql_many needs at least one fact")
    terms = ", ".join(
        f"({i}, cosineDistance(e.embedding, {vector_param(i)}))" for i in range(1, n + 1)
    )
    return f"""
        SELECT
            hit.1 - 1 AS qidx,
            m.memory_id, m.memory_key, m.value, m.pinned,
            m.agent, m.user_id, m.session_id, m.run_id, m.valid_at,
            hit.2 AS dist{_CANDIDATE_JOIN}
        ARRAY JOIN arrayFilter(
            x -> x.2 <= {{max_dists:Array(Float32)}}[x.1]
                 AND {{types:Array(String)}}[x.1] = m.memory_type
                 AND {{keys:Array(String)}}[x.1] != m.memory_key,
            [{terms}]) AS hit
        WHERE m.org = {{org:String}}
          AND m.namespace = {{ns:String}}
          AND m.tombstoned_at IS NULL
          AND m.status IN ('active', 'conflicted')
          AND e.tombstoned_at IS NULL
          AND e.embedding_dim = {{dim:UInt16}}
          AND length(e.embedding) = {{dim:UInt16}}{in_scope}
        ORDER BY qidx ASC, dist ASC
        LIMIT {PEER_LIMIT} BY qidx
        """


def _peer_candidates_sql(in_scope: str = "") -> str:
    """Stage A: the namespace's nearest vectors, nothing else read; `in_scope`
    is the read-scope clause (`scope_and`). No type filter: the memory row's type
    is the one that counts, and stage B applies it."""
    from server.core.vectors.search_settings import vector_search_settings

    return (
        """
        SELECT agent, user_id, session_id, memory_key,
               """
        + "cosineDistance(embedding, {emb:Array(Float32)})"
        + """ AS dist
        FROM loom.memory_embeddings FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND memory_key != {self_key:String}
          AND tombstoned_at IS NULL
          AND embedding_dim = {dim:UInt16}
          AND embedding.size0 = {dim:UInt16}"""
        + in_scope
        + """
        ORDER BY dist ASC
        LIMIT {k:UInt32}
        """
        + vector_search_settings(table="loom.memory_embeddings")
    )


# Stage B: the candidates' rows, by sort key, with the row filters.
_PEER_ROWS_SQL = """
        SELECT memory_id, memory_key, value, pinned,
               agent, user_id, session_id, run_id, valid_at
        FROM loom.memory FINAL
        PREWHERE org = {org:String}
          AND namespace = {ns:String}
          AND (agent, user_id, session_id, memory_key)
              IN {ids:Array(Tuple(String, String, String, String))}
        WHERE memory_type = {mt:String}
          AND tombstoned_at IS NULL
          AND status IN ('active', 'conflicted')
        """


# The pinned peers anywhere in the namespace: a pinned row outside the writer's
# read scope still blocks a close write (pinned is a namespace-wide invariant).
# The pinned rows' keys come from `loom.memory` first, so the memory row decides type
# and liveness (a vector row's own type is never read) and only those keys' vectors
# are read. Every pinned row is weighed by distance, not only the nearest few vectors.
def _pinned_row_filter(type_filter: str) -> str:
    """The pinned, live rows of the namespace, under `type_filter` (a predicate on `m`)."""
    return f"""m.org = {{org:String}}
          AND m.namespace = {{ns:String}}
          AND {type_filter}
          AND m.pinned = 1
          AND m.tombstoned_at IS NULL
          AND m.status IN ('active', 'conflicted')"""


_PINNED_ROW_FILTER = _pinned_row_filter(
    "m.memory_type = {mt:String}\n          AND m.memory_key != {self_key:String}"
)

PINNED_PEERS_SQL = f"""
        SELECT
            m.memory_id, m.memory_key, m.value, m.pinned,
            m.agent, m.user_id, m.session_id, m.run_id, m.valid_at,
            e.dist AS dist
        FROM loom.memory m FINAL
        JOIN (
            SELECT agent, user_id, session_id, memory_key,
                   cosineDistance(embedding, {{emb:Array(Float32)}}) AS dist
            FROM loom.memory_embeddings FINAL
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND (agent, user_id, session_id, memory_key) IN (
                  SELECT m.agent, m.user_id, m.session_id, m.memory_key
                  FROM loom.memory m FINAL
                  WHERE {_PINNED_ROW_FILTER})
              AND tombstoned_at IS NULL
              AND embedding_dim = {{dim:UInt16}}
              AND length(embedding) = {{dim:UInt16}}
        ) e
          ON  e.agent = m.agent AND e.user_id = m.user_id
          AND e.session_id = m.session_id AND e.memory_key = m.memory_key
        WHERE {_PINNED_ROW_FILTER}
          AND e.dist <= {{max_dist:Float32}}
        ORDER BY dist ASC
        LIMIT {PEER_LIMIT}
        """


def pinned_sql_many(n: int) -> str:
    """The batched form of `PINNED_PEERS_SQL`, for a narrowed bulk writer: the
    pinned peers of `n` facts anywhere in the namespace, at most `PEER_LIMIT` per
    fact. Its own statement, so pinned rows never compete with the writer's own
    rows for those places, and it reads only the pinned rows' vectors."""
    if n < 1:
        raise ValueError("pinned_sql_many needs at least one fact")
    terms = ", ".join(
        f"({i}, cosineDistance(embedding, {vector_param(i)}))" for i in range(1, n + 1)
    )
    # Each fact's own type and key are matched per fact in the ARRAY JOIN below.
    row_filter = _pinned_row_filter("m.memory_type IN {types:Array(String)}")
    return f"""
        SELECT
            hit.1 - 1 AS qidx,
            m.memory_id, m.memory_key, m.value, m.pinned,
            m.agent, m.user_id, m.session_id, m.run_id, m.valid_at,
            hit.2 AS dist
        FROM loom.memory m FINAL
        JOIN (
            SELECT agent, user_id, session_id, memory_key,
                   arrayFilter(x -> x.2 <= {{max_dists:Array(Float32)}}[x.1], [{terms}]) AS hits
            FROM loom.memory_embeddings FINAL
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND (agent, user_id, session_id, memory_key) IN (
                  SELECT m.agent, m.user_id, m.session_id, m.memory_key
                  FROM loom.memory m FINAL
                  WHERE {row_filter})
              AND tombstoned_at IS NULL
              AND embedding_dim = {{dim:UInt16}}
              AND length(embedding) = {{dim:UInt16}}
        ) e
          ON  e.agent = m.agent AND e.user_id = m.user_id
          AND e.session_id = m.session_id AND e.memory_key = m.memory_key
        ARRAY JOIN arrayFilter(
            x -> {{types:Array(String)}}[x.1] = m.memory_type
                 AND {{keys:Array(String)}}[x.1] != m.memory_key,
            e.hits) AS hit
        WHERE {row_filter}
        ORDER BY qidx ASC, dist ASC
        LIMIT {PEER_LIMIT} BY qidx
        """
