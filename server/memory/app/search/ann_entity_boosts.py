"""ANN entity-boost lookup over entity_vectors, for memory.search."""

from __future__ import annotations

import logging
from typing import Any

from server.core.ch import get_client
from server.core.vectors.search_settings import vector_search_settings
from server.observability.otel import traced

log = logging.getLogger(__name__)


async def _ann_entity_boosts(
    *,
    org: str,
    namespaces: list[str],
    agent: str,
    user_id: str | None,
    query_entities: list[str],
    embedder: Any,
) -> dict[tuple[str, str], float]:
    """Compute per-(namespace, memory_key) entity-boost via ANN over entity_vectors.

    For each query entity, embed it and run cosineDistance against the
    entity_vectors table. Matches above a 0.5 similarity threshold
    contribute boost weighted by similarity, attenuated when a query
    entity links to many memories (the spread factor).

    Returns: (namespace, memory_key) → cumulative boost (capped per-key
    at 0.5 so a single strong entity match can't dominate the composite rank).
    """
    import asyncio as _asyncio

    if not query_entities:
        return {}
    ch = get_client()

    from server.settings import settings as _s

    top_entities = int(_s.memory_search_entity_boost_top_entities)
    max_pairs = int(_s.memory_search_entity_boost_max_pairs)

    async def _one_entity(entity: str) -> list[tuple[str, str, float]]:
        emb = await embedder.embed(entity)
        # Two stages, because `loom.entity_vectors` stores one row per
        # (entity_text, memory_key) EDGE with the entity's vector copied onto
        # every edge. A flat `ORDER BY distance LIMIT 20` therefore returns 20
        # edges, not 20 entities — in the worst case the whole budget goes to
        # a single well-connected entity while semantically closer but
        # less-connected entities never appear.
        # Stage 1 ranks DISTINCT entities by their best distance; stage 2
        # expands the winners to their edges. `max_pairs` bounds the expansion,
        # since a hub entity can carry hundreds of edges.
        # This deliberately forfeits CH's vector-search index rewrite: the
        # GROUP BY breaks the required plan shape (see the note in
        # search_sql.py). That is the right trade here — the table is one row
        # per entity-edge in one namespace, small enough that no index is
        # warranted, and correctness of the candidate set matters more than a
        # rewrite that would never fire on this table anyway.
        sql = f"""
            WITH top_ents AS (
                SELECT entity_text
                FROM loom.entity_vectors FINAL
                WHERE org = {{org:String}}
                  AND namespace IN {{namespaces:Array(String)}}
                  AND agent = {{agent:String}}
                  AND user_id = {{uid:String}}
                  AND embedding_dim > 0
                  AND tombstoned_at IS NULL
                GROUP BY entity_text
                ORDER BY min(
                    cosineDistance(entity_embedding, {{q:Array(Float32)}})
                ) ASC
                LIMIT {top_entities}
            )
            SELECT namespace,
                   memory_key,
                   cosineDistance(entity_embedding, {{q:Array(Float32)}}) AS distance
            FROM loom.entity_vectors FINAL
            WHERE org = {{org:String}}
              AND namespace IN {{namespaces:Array(String)}}
              AND agent = {{agent:String}}
              AND user_id = {{uid:String}}
              AND embedding_dim > 0
              AND tombstoned_at IS NULL
              AND entity_text IN (SELECT entity_text FROM top_ents)
            ORDER BY distance ASC
            LIMIT {max_pairs}
            {vector_search_settings(table="loom.entity_vectors", column="entity_embedding")}
        """
        # The boost runs as a task beside the vector legs, so it is charged to no `stage_ms`
        # stage; this span is the only place its wall is attributable.
        try:
            with traced(
                "ch.query.entity_vectors",
                op="entity_boost",
                entities=len(query_entities),
                namespaces=len(namespaces),
            ):
                rows = await ch.query_all(
                    sql,
                    params={
                        "q": emb,
                        "org": org,
                        "namespaces": list(namespaces),
                        "agent": agent,
                        "uid": user_id or "-",
                    },
                )
        except Exception as e:  # noqa: BLE001 — entity boost is optional; search runs without it
            log.warning("entity_vectors lookup failed for %r: %s", entity, e)
            return []
        return [
            (str(r["namespace"]), str(r["memory_key"]), sim)
            for r in rows
            if (sim := 1.0 - float(r["distance"])) >= 0.5
        ]

    per_entity = await _asyncio.gather(
        *[_one_entity(e) for e in query_entities],
    )

    # Aggregate by entity fan-out. A query entity that matches many
    # memories is a hub-like weak signal, so each hit from that entity is
    # attenuated by spread before accumulating per memory_key. Because stage 2 expands every
    # edge, `spread` is the entity's true fan-out: a hub entity with 461 edges attenuates to
    # 0.005, which is the intent (a hub entity is a weak signal).
    by_key: dict[tuple[str, str], float] = {}
    for hits in per_entity:
        spread = len(hits)
        attenuation = 1.0 / (1.0 + 0.001 * (spread - 1) ** 2)
        for namespace, memory_key, sim in hits:
            key = (namespace, memory_key)
            by_key[key] = by_key.get(key, 0.0) + 0.5 * sim * attenuation

    return {key: min(boost, 0.5) for key, boost in by_key.items()}
