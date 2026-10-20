"""The analytics export's statements: one day of the operation ledger and a
snapshot of the namespaces, each read as one Parquet file.

The columns are an allowlist. A file leaves Loom for a warehouse that erasure
never reaches, so nothing here carries memory content (keys, values, the
`args` map) or names a person (`user_id`, `session_id`, `subject`,
`owner_email`): users and sessions go out as counts only."""

from __future__ import annotations

from datetime import date

from server.core.ch import CHClient, get_client

# One row per (org, namespace, agent, operation, kind, transport, traffic
# class) of UTC day {day:Date}. `kind` is '' for a request and 'worker' for a
# worker pass; `users` and `sessions` leave out the '-' of a call that named
# none. The `ts` bounds prune to the day's monthly partition, which a daily
# batch reads once (the ledger is a plain MergeTree: no FINAL).
USAGE_SQL = """
SELECT
    org, namespace, agent, operation, kind, transport, traffic_class,
    {day:Date} AS day,
    count() AS ops,
    countIf(NOT success) AS errors,
    countIf(status_class = 'server') AS server_errors,
    countIf(budget_ms > 0 AND latency_ms > budget_ms) AS slow,
    sum(toUInt64(input_tokens)) AS input_tokens,
    sum(toUInt64(output_tokens)) AS output_tokens,
    sum(toUInt64(embedding_tokens)) AS embedding_tokens,
    sum(toUInt64(llm_calls)) AS llm_calls,
    sum(upstream_cost_usd) AS cost_usd,
    sum(bytes_written) AS bytes_written,
    toUInt32(quantiles(0.5, 0.95, 0.99)(latency_ms)[1]) AS latency_p50_ms,
    toUInt32(quantiles(0.5, 0.95, 0.99)(latency_ms)[2]) AS latency_p95_ms,
    toUInt32(quantiles(0.5, 0.95, 0.99)(latency_ms)[3]) AS latency_p99_ms,
    uniqExactIf(user_id, user_id != '-') AS users,
    uniqExactIf(session_id, session_id != '-') AS sessions,
    now64(3, 'UTC') AS snapshot_at
FROM loom.agent_events
WHERE ts >= toDateTime64({day:Date}, 3, 'UTC')
  AND ts < toDateTime64({day:Date} + 1, 3, 'UTC')
GROUP BY org, namespace, agent, operation, kind, transport, traffic_class
ORDER BY org, namespace, agent, operation, kind, transport, traffic_class
"""

# One row per (org, namespace) that is registered or holds a live memory row:
# the registry's description of it and its live memory count, as of
# `snapshot_at` whatever {day:Date} is. An unregistered namespace has
# `registered = false` and NULL registry columns.
NAMESPACES_SQL = """
SELECT
    org, namespace,
    n.registered AS registered,
    if(n.registered, n.kind, NULL) AS kind,
    if(n.registered, toString(n.visibility), NULL) AS visibility,
    if(n.registered, n.active, NULL) AS active,
    if(n.registered, n.created_at, NULL) AS created_at,
    m.live_memories AS live_memories,
    if(m.live_memories > 0, m.last_memory_at, NULL) AS last_memory_at,
    {day:Date} AS day,
    now64(3, 'UTC') AS snapshot_at
FROM (SELECT org, namespace, true AS registered, kind, visibility, active, created_at
      FROM loom.namespaces FINAL) AS n
FULL OUTER JOIN (SELECT org, namespace, count() AS live_memories,
                        max(created_at) AS last_memory_at
                 FROM loom.memory FINAL
                 WHERE tombstoned_at IS NULL
                 GROUP BY org, namespace) AS m
  USING (org, namespace)
ORDER BY org, namespace
"""

ENTITIES = {"usage": USAGE_SQL, "namespaces": NAMESPACES_SQL}


async def parquet(entity: str, day: date, *, ch: CHClient | None = None) -> bytes:
    """`entity`'s rows for UTC day `day`, as one Parquet file."""
    ch = ch or get_client()
    return await ch.raw_bytes(f"{ENTITIES[entity]}FORMAT Parquet", {"day": day})
