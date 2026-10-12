"""The statements a conflict check reads its peer candidates with: the batched
join over a namespace's vectors, and the two-stage nearest-vectors /
rows-by-sort-key pair."""

from __future__ import annotations

_CANDIDATE_JOIN = """
        FROM loom.memory m FINAL
        JOIN loom.memory_embeddings e FINAL
          ON  e.org = m.org AND e.namespace = m.namespace
          AND e.memory_key = m.memory_key
          AND e.user_id = m.user_id AND e.session_id = m.session_id
          AND e.agent = m.agent"""


def candidate_sql_many(n: int, in_scope: str = "", limit: int = 10) -> str:
    """The batched form of `check_on_commit`'s peer statement:
    one read of the namespace's vectors serves `n` facts. Each fact's distance
    is its own `cosineDistance` term — an `arrayMap` over the vector column
    replicates the 6 KB vector once per fact per row (1.7 GB peak at n=12);
    the unrolled tuple array does not. `ARRAY JOIN` over the filtered tuples
    drops rows that match no fact, the per-fact `memory_type` / self-key
    predicates and distance ceiling ride in the filter, and `LIMIT 10 BY qidx`
    is the per-fact statement's `LIMIT 10`. Measured identical per-fact lists
    at n = 1, 4, 6, 8, 12, 24 against the per-fact statement. `in_scope`: the
    read-scope clause on `m`, so other users' rows never take the 10 places
    (their pinned rows come from this statement unscoped). `limit`: peers per fact."""
    if n < 1:
        raise ValueError("candidate_sql_many needs at least one fact")
    terms = ", ".join(
        f"({i}, cosineDistance(e.embedding, {{embs:Array(Array(Float32))}}[{i}]))"
        for i in range(1, n + 1)
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
        LIMIT {int(limit)} BY qidx
        """


def _peer_candidates_sql(in_scope: str = "") -> str:
    """Stage A: the namespace's nearest vectors, nothing else read; `in_scope`
    is the read-scope clause (`scope_and`)."""
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
