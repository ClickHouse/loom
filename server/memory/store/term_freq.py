"""Repository for the BM25 document-frequency aggregates, `loom.chunk_term_freq`
(knowledge chunks) and `loom.memory_term_freq` (memory excerpts).

The term-frequency views only see INSERTs. Supersession (knowledge) and
tombstoning (memory) are ALTER UPDATEs the views never observe, so document
frequency accumulates the vocabulary of rows that are no longer live. A
rebuild deletes one namespace's aggregate rows and re-inserts them from the
live rows with the views' own SELECT (a knowledge chunk's terms are those of its
context and its text). `loom.memory_embeddings` is a
ReplacingMergeTree: without FINAL an older, untombstoned version of a
tombstoned row still counts as live.
"""

from __future__ import annotations

from typing import Any

from server.core import bm25
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

PLANES = ("knowledge", "memory")

_REBUILD_SQL: dict[str, tuple[str, str]] = {
    "knowledge": (
        "loom.chunk_term_freq",
        """
        INSERT INTO loom.chunk_term_freq (org, namespace, term, df_state, df_added_at)
        SELECT org, namespace, term, uniqExactState((source_uri, chunk_idx)) AS df_state, now()
        FROM loom.chunks
        ARRAY JOIN """
        + bm25.terms_sql("concat(context, ' ', content)")
        + """ AS term
        WHERE org = {org:String} AND namespace = {ns:String}
          AND content != '' AND """
        + LIVE_CHUNK_SQL
        + """
        GROUP BY org, namespace, term
        """,
    ),
    "memory": (
        "loom.memory_term_freq",
        """
        INSERT INTO loom.memory_term_freq (org, namespace, term, df_state, df_added_at)
        SELECT org, namespace, term,
               uniqExactState((toString(agent), user_id, session_id, toString(scope),
                               toString(embedding_model), memory_key)) AS df_state,
               now()
        FROM loom.memory_embeddings FINAL
        ARRAY JOIN """
        + bm25.terms_sql("content_excerpt")
        + """ AS term
        WHERE org = {org:String} AND namespace = {ns:String}
          AND embedding_dim > 0 AND tombstoned_at IS NULL
        GROUP BY org, namespace, term
        """,
    ),
}


async def rebuild(
    ch: Any, *, org: str, namespace: str, planes: tuple[str, ...] = PLANES
) -> dict[str, int]:
    """Delete-then-reinsert the term-frequency aggregate of (org, namespace) for
    each plane from its live rows. Returns the aggregate rows deleted per plane."""
    deleted: dict[str, int] = {}
    params = {"org": org, "ns": namespace}
    for plane in planes:
        table, insert_sql = _REBUILD_SQL[plane]
        row = await ch.query_one(
            f"SELECT count() AS n FROM {table} "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            params=params,
        )
        deleted[plane] = int((row or {}).get("n") or 0)
        if deleted[plane]:
            await ch.execute(
                f"ALTER TABLE {table} DELETE "
                "WHERE org = {org:String} AND namespace = {ns:String} "
                "SETTINGS mutations_sync = 2",
                params=params,
            )
        await ch.execute(insert_sql, params=params)
    return deleted
