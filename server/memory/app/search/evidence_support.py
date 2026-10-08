"""Belief-source support count for a memory.search hit pool."""

from __future__ import annotations

from typing import Any

from server.core.ch import get_client
from server.memory.api.models_search import MemorySearchInput


async def _evidence_support_count(input: MemorySearchInput, hits: list[Any]) -> int:
    if not hits:
        return 0
    ns_keys: dict[str, list[str]] = {}
    for h in hits:
        ns_keys.setdefault(h.namespace or input.namespace, []).append(h.memory_key)
    from server.observability.otel import traced

    with traced("ch.query.memory_belief_sources", op="evidence_support", keys=len(hits)):
        rows = await get_client().query_all(
            """
        SELECT count() AS n
        FROM (
            SELECT memory_key, evidence_id, argMax(tombstoned_at, created_at) AS tomb_latest
            FROM loom.memory_belief_sources
            WHERE org = {org:String}
              AND namespace IN {namespaces:Array(String)}
              AND memory_key IN {keys:Array(String)}
            GROUP BY memory_key, evidence_id
            HAVING tomb_latest IS NULL
        )
        """,
            params={
                "org": input.org,
                "namespaces": list(ns_keys.keys()),
                "keys": [k for keys in ns_keys.values() for k in keys],
            },
        )
    return int(rows[0]["n"]) if rows else 0
