"""The ops plane's statements: the deployment self-checks' reads of
`system.parts` / `system.part_log`, the pending embedding stubs, and the op mix
over `agent_events`."""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client


async def active_parts_by_table(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """The 20 loom tables with the most active parts, with their row counts."""
    ch = ch or _default_client()
    return list(
        await ch.query_all(
            """
            SELECT table, count() AS parts, sum(rows) AS rows
            FROM system.parts
            WHERE active AND database = currentDatabase()
            GROUP BY table
            ORDER BY parts DESC
            LIMIT 20
            """
        )
    )


async def failed_merges_15m(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Per loom table, the merges that failed in the last quarter hour. Raises
    where `system.part_log` does not exist yet (replica-local, created on its
    first flush)."""
    ch = ch or _default_client()
    return list(
        await ch.query_all(
            """
                SELECT table, count() AS failures, any(error) AS error_code,
                       any(merge_reason) AS merge_reason, max(event_time) AS last_failure
                FROM system.part_log
                WHERE database = currentDatabase() AND event_type = 'MergeParts' AND error != 0
                  AND event_time > now() - INTERVAL 15 MINUTE
                GROUP BY table
                ORDER BY failures DESC
                LIMIT 10
                """
        )
    )


async def embedding_stub_count(
    org: str, namespace: str | None = None, *, ch: _CHClient | None = None
) -> int:
    """Live memory embedding rows still waiting for their vector (`embedding_dim = 0`)."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org}
    ns_filter = ""
    if namespace:
        ns_filter = "AND namespace = {ns:String}"
        params["ns"] = namespace
    row = await ch.query_one(
        f"""
            SELECT count() AS n
            FROM loom.memory_embeddings FINAL
            WHERE org = {{org:String}}
              {ns_filter}
              AND embedding_dim = 0
              AND tombstoned_at IS NULL
            """,
        params=params,
    )
    return int(row["n"]) if row else 0


async def memory_op_counts(
    org: str, namespace: str | None, window_days: int, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Successful memory-plane operations per name over the window (at most 200),
    each with up to 50 of the agents that called it."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "days": int(window_days)}
    ns_filter = ""
    if namespace:
        ns_filter = "AND namespace = {ns:String}"
        params["ns"] = namespace
    return list(
        await ch.query_all(
            f"""
        SELECT operation, count() AS c, groupUniqArray(50)(agent) AS agents
        FROM loom.agent_events
        WHERE org = {{org:String}}
          {ns_filter}
          AND ts > now() - INTERVAL {{days:UInt16}} DAY
          AND success
          AND (operation LIKE 'memory.%' OR operation LIKE 'working_memory.%'
               OR operation LIKE 'context.%' OR operation LIKE 'facts.%')
        GROUP BY operation
        ORDER BY c DESC
        LIMIT 200
        """,
            params=params,
        )
    )
