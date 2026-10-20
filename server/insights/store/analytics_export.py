"""The analytics export's statements: one day of the operation ledger and a
snapshot of the namespaces, each read as one Parquet file.

The columns are an allowlist. A file leaves Loom for a warehouse that erasure
never reaches, so nothing here carries memory content (keys, values, the
`args` map) or names a person (`user_id`, `session_id`, `subject`,
`owner_email`): users and sessions go out as counts only. Org, namespace and
agent names can name a person too (a personal namespace is named after its
owner's email, self-signup after a username), so they leave as a keyed hash: a
pseudonym that the holder of the key can recompute."""

from __future__ import annotations

from datetime import date

from server.core.ch import CHClient

# One row per (org, namespace, agent, operation, kind, transport, traffic
# class) of UTC day {day:Date}. `kind` is 'request' or 'worker' (a worker
# pass); `users` and `sessions` leave out the '-' of a call that named none.
# A `/v1/me/*` row's operation is built from the request path, so it can hold
# any string a caller sent: every `me.*` operation leaves as `me`. That happens
# in the inner query, so the GROUP BY can only mean the collapsed operation,
# whatever `prefer_column_name_to_alias` says. The `ts` bounds prune to the
# day's monthly partition, which a daily batch reads once (the ledger is a plain
# MergeTree: no FINAL).
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
FROM (SELECT * REPLACE (if(startsWith(operation, 'me.'), 'me', operation) AS operation)
      FROM loom.agent_events
      WHERE ts >= toDateTime64({day:Date}, 3, 'UTC')
        AND ts < toDateTime64({day:Date} + 1, 3, 'UTC'))
GROUP BY org, namespace, agent, operation, kind, transport, traffic_class
"""

# One row per (org, namespace) that is registered or holds a live memory row:
# the registry's description of it and its live memory count, as of
# `snapshot_at` whatever {day:Date} is. An unregistered namespace has
# `registered = false` and NULL registry columns. Times leave as days: a
# millisecond beside a hashed personal namespace could be matched to the
# person who signed up or wrote then. The `ifNull`s give the same
# answer whether the FULL JOIN fills a missing side with defaults or with NULLs
# (`join_use_nulls`).
NAMESPACES_SQL = """
SELECT
    org, namespace,
    ifNull(n.registered, false) AS registered,
    if(registered, n.kind, NULL) AS kind,
    if(registered, toString(n.visibility), NULL) AS visibility,
    if(registered, n.active, NULL) AS active,
    if(registered, toDate(n.created_at), NULL) AS created_on,
    ifNull(m.live_memories, 0) AS live_memories,
    if(live_memories > 0, toDate(m.last_memory_at), NULL) AS last_memory_on,
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
"""

ENTITIES = {"usage": USAGE_SQL, "namespaces": NAMESPACES_SQL}

# The columns of each entity that hold a name, and leave hashed.
NAME_COLUMNS = {"usage": ("org", "namespace", "agent"), "namespaces": ("org", "namespace")}

# Each file's row order. The statements above carry none: rows sorted by the
# stored names would line up with a sorted list of candidate names, and give
# the names back without the key. The file is sorted by the hashes instead.
ROW_ORDER = {
    "usage": "org, namespace, agent, operation, kind, transport, traffic_class",
    "namespaces": "org, namespace",
}


def name_hash_sql(expr: str) -> str:
    """The lowercase hex of `expr`'s SipHash-2-4-128 under the key {k0:UInt64},
    {k1:UInt64}: the reference variant, which any SipHash-128 library reproduces.
    A plain String, so the file's dictionary pages are built from the hashes in
    row order and never depend on how ClickHouse keeps a `LowCardinality` one."""
    sip = f"sipHash128ReferenceKeyed(({{k0:UInt64}}, {{k1:UInt64}}), {expr})"
    return f"CAST(lower(hex({sip})) AS String)"


def _hashed(entity: str) -> str:
    """`entity`'s statement with each name column replaced by its hash, sorted
    by the hashes. The sort is outside the subquery that hashes, so its names
    can only mean the hashes, whatever `prefer_column_name_to_alias` says."""
    replace = ", ".join(f"{name_hash_sql(c)} AS {c}" for c in NAME_COLUMNS[entity])
    hashed = f"SELECT * REPLACE ({replace}) FROM ({ENTITIES[entity]})"
    return f"SELECT * FROM ({hashed}) ORDER BY {ROW_ORDER[entity]}"


async def parquet(entity: str, day: date, *, name_key: tuple[int, int], ch: CHClient) -> bytes:
    """`entity`'s rows for UTC day `day`, as one Parquet file, names hashed under `name_key`."""
    params = {"day": day, "k0": name_key[0], "k1": name_key[1]}
    return await ch.raw_bytes(f"{_hashed(entity)} FORMAT Parquet", params)
