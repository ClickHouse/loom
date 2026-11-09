"""The statements a conflict check reads its peer candidates with: the two-stage
nearest-vectors / rows-by-sort-key pair, and the pinned-rows-first pair a narrowed
writer adds."""

from __future__ import annotations

# The most peers a conflict check weighs per fact.
PEER_LIMIT = 10


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
_PINNED_ROW_FILTER = """m.org = {org:String}
          AND m.namespace = {ns:String}
          AND m.memory_type = {mt:String}
          AND m.memory_key != {self_key:String}
          AND m.pinned = 1
          AND m.tombstoned_at IS NULL
          AND m.status IN ('active', 'conflicted')"""

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
