"""namespace.summary — at-a-glance rollup.

Pure compose endpoint: three CH aggregates over existing tables, run
concurrently.
No new storage. Read-only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.core.ch import get_client
from server.identity.api.models_namespace_catalog import (
    NamespaceSummaryInput,
    NamespaceSummaryOutput,
    _NamespaceTopEntity,
    _NamespaceTypeCount,
)
from server.memory.domain.graph_quality import is_lexical_junk_entity
from server.observability.consumer import INSPECTOR_CONSUMERS


def _memory_changes() -> list[str]:
    """The memory ops that change a row (a writer's, or an audited admin's): an inspector's
    call to one is a person using the namespace. An admin's read (`memory.dedup_list`) is
    not one, though no viewer may call it."""
    from server.operations.base import Role
    from server.operations.registry import ALL_OPERATIONS

    return sorted(
        op.name
        for op in ALL_OPERATIONS
        if op.name.startswith("memory.")
        and not op.read_only
        and (op.audit or op.required_role == Role.WRITER)
    )


async def handle_namespace_summary(input: NamespaceSummaryInput) -> NamespaceSummaryOutput:
    ch = get_client()
    params = {"org": input.org, "ns": input.namespace}

    async def _types() -> list[dict[str, Any]]:
        return await ch.query_all(
            """
            SELECT memory_type, count() AS n
            FROM loom.memory FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND tombstoned_at IS NULL
            GROUP BY memory_type
            ORDER BY n DESC
            """,
            params=params,
        )

    async def _entities() -> list[dict[str, Any]]:
        if input.top_entities_limit <= 0:
            return []
        # Degree is read from the live edges, not the loom.entity_degree rollup: the rollup
        # sums every edge ever inserted and never forgets one whose `invalid_at` was stamped
        # or that the 180-day TTL dropped, so a retired node would stay "top" forever. The
        # live read costs ~165 ms over 1.9M edges. Over-fetch so the lexical-junk filter
        # below still fills the limit.
        return await ch.query_all(
            """
            SELECT node AS entity, sum(co_occur) AS degree
            FROM loom.entity_edges
            ARRAY JOIN [entity_a, entity_b] AS node
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND invalid_at IS NULL
            GROUP BY node
            ORDER BY degree DESC
            LIMIT {lim:UInt32}
            """,
            params={**params, "lim": int(input.top_entities_limit) * 2},
        )

    async def _activity() -> dict[str, Any] | None:
        return await ch.query_one(
            """
            SELECT
                countIf(operation IN ('memory.set', 'memory.bulk_set',
                                       'memory.set_from_messages',
                                       'memory.update_by_id',
                                       'memory.touch'))   AS writes,
                countIf(operation = 'memory.search')      AS recalls,
                uniqExact(agent)                          AS agents,
                max(ts)                                   AS last_at
            FROM loom.agent_events
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND ts >= now() - INTERVAL {win:UInt32} SECOND
              -- the dashboard's and the assistant's reads inspect the namespace, not use it;
              -- a change to a memory from the dashboard (a set, a delete, a pin) uses it
              AND (operation IN {memory_changes:Array(String)}
                   OR consumer NOT IN {inspectors:Array(String)})
            """,
            params={
                **params,
                "win": int(input.activity_seconds),
                "inspectors": list(INSPECTOR_CONSUMERS),
                "memory_changes": _memory_changes(),
            },
        )

    type_rows, entity_rows, activity_row = await asyncio.gather(_types(), _entities(), _activity())
    type_counts = [
        _NamespaceTypeCount(memory_type=str(r["memory_type"]), n=int(r["n"])) for r in type_rows
    ]
    total_memories = sum(t.n for t in type_counts)
    top_entities = [
        _NamespaceTopEntity(entity=str(r["entity"]), degree=int(r["degree"]))
        for r in entity_rows
        if not is_lexical_junk_entity(str(r["entity"]))
    ][: input.top_entities_limit]
    if activity_row is None:
        activity_row = {"writes": 0, "recalls": 0, "agents": 0, "last_at": None}

    return NamespaceSummaryOutput(
        org=input.org,
        namespace=input.namespace,
        total_memories=total_memories,
        type_counts=type_counts,
        top_entities=top_entities,
        recent_writes=int(activity_row["writes"]),
        recent_recalls=int(activity_row["recalls"]),
        distinct_agents=int(activity_row["agents"]),
        last_activity_at=activity_row.get("last_at") or None,
    )
