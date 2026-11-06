"""loom.memory_served_daily: how often an agent read returned a memory.

One row per key an agent read op returned, on the newest live row of the key inside
the reader's scope (`usage.newest_key_ids`); the SummingMergeTree folds them into one
row per (memory, agent, day). Apart from `loom.memory_usage_events`, whose recall credits
rank: nothing ranks on this table and the usage fold never reads it.
"""

from __future__ import annotations

from typing import Any

from server.core.ch_writes import async_insert_settings
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters, scope_and
from server.memory.store.usage import newest_key_ids

TABLE = "loom.memory_served_daily"


async def record_served(
    org: str,
    ns_keys: dict[str, list[str]],
    *,
    agent: str,
    scope: ReadFilters = WHOLE_NAMESPACE,
    ch: _CHClient | None = None,
) -> int:
    """One served row per (namespace, memory_key) that resolves to a live row inside
    `scope`; a tombstoned or unknown key is skipped. Returns the rows written."""
    from server.core.repo_base import _default_client

    ch = ch or _default_client()
    rows = await newest_key_ids(org, ns_keys, scope=scope, ch=ch)
    if not rows:
        return 0
    now = naive_utc_now().replace(microsecond=0)
    await ch.insert(
        TABLE,
        [
            {
                "org": org,
                "namespace": str(r["namespace"]),
                "memory_id": str(r["memory_id"]),
                "agent": agent,
                "day": now.date(),
                "served": 1,
                "last_at": now,
            }
            for r in rows
        ],
        settings=async_insert_settings(background=True),
    )
    return len(rows)


# A key's served rows per agent, over its live rows inside the reader's scope (an event
# lands on the newest copy inside the scope of the read that served it). Keyed on
# (org, namespace, memory_id) for the few ids of the key.
_BY_AGENT_SQL = """
    SELECT agent,
           sum(served) AS n,
           sumIf(served, day > today() - 30) AS n_30d,
           max(last_at) AS last_at
    FROM loom.memory_served_daily
    WHERE org = {org:String}
      AND namespace = {ns:String}
      AND memory_id IN (
          SELECT memory_id FROM loom.memory FINAL
          WHERE org = {org:String} AND namespace = {ns:String}
            AND memory_key = {key:String} AND tombstoned_at IS NULL{scope}
      )
    GROUP BY agent
"""


async def served_by_agent(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    scope: ReadFilters = WHOLE_NAMESPACE,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Agent reads that returned a key, one row per agent: `n` all time, `n_30d`,
    `last_at`, over the key's live rows inside `scope` (the reader's)."""
    from server.core.repo_base import _default_client

    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "key": memory_key}
    sql = _BY_AGENT_SQL.replace("{scope}", scope_and(scope, params))
    return await ch.query_all(sql, params=params)
