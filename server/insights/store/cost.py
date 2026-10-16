"""The cost plane's statements: the daily roll-up (`agent_cost_rollup_daily`,
refreshed once a minute) with the live tail of `agent_events` newer than its
mark, the 24 h spend reads behind `cost.budget_status`, and the agent turn
ledger (`agent_turn_usage`). The handlers decide scope and fold the rows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client

# The roll-up's grain and state columns, and the same
# aggregation as the refreshable view's SELECT, for the live tail.
ROLLUP_DIMS = "org, namespace, agent, day, operation"
ROLLUP_STATES = (
    "cost_usd_state, tokens_state, embed_tokens_state, ops_state, errors_state, "
    "latency_sum_state, latency_quantile_state"
)
TAIL_STATES = (
    "sumState(upstream_cost_usd) AS cost_usd_state, "
    "sumState(toUInt64(input_tokens) + toUInt64(output_tokens)) AS tokens_state, "
    "sumState(toUInt64(embedding_tokens)) AS embed_tokens_state, "
    "countState() AS ops_state, "
    "sumStateIf(toUInt64(1), success = false) AS errors_state, "
    "sumState(toUInt64(latency_ms)) AS latency_sum_state, "
    "quantilesState(0.5, 0.95, 0.99)(latency_ms) AS latency_quantile_state"
)

# The exact-prompt response cache's ledger reading. memory.ask rows carry
# `out_llm_cache_hits` and `out_llm_cache_saved_usd` in `args` (event_args projection); a row
# without the key (other ops) reads 0 under `toUInt64OrZero` / `toFloat64OrZero`. The roll-up
# has no args, so on that path this is its own statement over the window's memory.ask rows
# (bounded by the same org / namespace / date predicates as the readout).
CACHE_SELECT = (
    "sum(toUInt64OrZero(args['out_llm_cache_hits']))       AS cache_hits, "
    "sum(toFloat64OrZero(args['out_llm_cache_saved_usd'])) AS cache_saved_usd"
)

# The optional equality filters of a summary: (column, bound parameter).
_SUMMARY_FILTERS = (
    ("agent", "agent"),
    ("operation", "op"),
    ("user_id", "uid"),
    ("traffic_class", "tclass"),
    ("instance_label", "ilabel"),
)

TURN_USAGE_COLUMNS = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "message_id",
    "consumer",
    "model",
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
    "output_tokens",
    "cost_usd",
    "priced",
    "turn_at",
)


def summary_where(
    org: str,
    from_date: date,
    to_date: date,
    *,
    namespace: str | None,
    scope_where: str,
    scope_params: dict[str, Any],
    filters: dict[str, str | None],
) -> tuple[str, dict[str, Any]]:
    """The predicate every summary statement shares, after `org` and the date
    window: the namespace when one is named, else the caller's scope; then each
    set equality filter (`agent`, `operation`, `user_id`, `traffic_class`,
    `instance_label`)."""
    where = ""
    params: dict[str, Any] = {"org": org, "from_d": from_date, "to_d": to_date}
    if namespace:
        where += " AND namespace = {ns:String}"
        params["ns"] = namespace
    else:
        where += scope_where
        params.update(scope_params)
    for column, name in _SUMMARY_FILTERS:
        value = filters.get(column)
        if value is not None:
            where += f" AND {column} = {{{name}:String}}"
            params[name] = value
    return where, params


async def summary_rows(
    where: str,
    params: dict[str, Any],
    *,
    dims: Sequence[str],
    scan: bool,
    include_cache: bool,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The summary grouped by `dims`, at most 5000 rows by ops. `scan` reads
    `agent_events` directly (the events-only dimensions and filters; with
    `include_cache` the cache columns too); otherwise the roll-up's states for
    the window UNION ALL the same aggregation over the events newer than its
    high-water mark, merged — exact at any instant, and the live part reads at
    most one refresh period of events."""
    ch = ch or _default_client()
    group_by_sql = ", ".join(dims)
    if scan:
        # per-event rows: plain sum/count/quantiles; `day = toDate(ts)` keeps day grouping
        select_dims_scan = ", ".join(
            "toDate(ts) AS day"
            if d == "day"
            else ("traffic_class AS traffic_class" if d == "traffic_class" else d)
            for d in dims
        )
        base_select = (
            "sum(upstream_cost_usd)                          AS cost_usd, "
            "sum(toUInt64(input_tokens) + toUInt64(output_tokens)) AS tokens, "
            "sum(toUInt64(embedding_tokens))                 AS embed_tokens, "
            "count()                                         AS ops, "
            "countIf(success = false)                        AS errors, "
            "quantiles(0.5, 0.95, 0.99)(latency_ms)          AS lat_q"
            + (", " + CACHE_SELECT if include_cache else "")
        )
        sql = f"""
            SELECT {select_dims_scan + (", " if select_dims_scan else "")}{base_select}
            FROM loom.agent_events
            WHERE org = {{org:String}}
              AND toDate(ts) BETWEEN {{from_d:Date}} AND {{to_d:Date}}
              {where}
            {("GROUP BY " + group_by_sql) if group_by_sql else ""}
            ORDER BY ops DESC
            LIMIT 5000
        """
    else:
        select_dims = ", ".join(dims)
        base_select = (
            "sumMerge(cost_usd_state)      AS cost_usd, "
            "sumMerge(tokens_state)        AS tokens, "
            "sumMerge(embed_tokens_state)  AS embed_tokens, "
            "countMerge(ops_state)         AS ops, "
            "sumMerge(errors_state)        AS errors, "
            "quantilesMerge(0.5, 0.95, 0.99)(latency_quantile_state) AS lat_q"
        )
        sql = f"""
            SELECT {select_dims + (", " if select_dims else "")}{base_select}
            FROM (
                SELECT {ROLLUP_DIMS}, {ROLLUP_STATES}
                FROM loom.agent_cost_rollup_daily
                WHERE org = {{org:String}}
                  AND day BETWEEN {{from_d:Date}} AND {{to_d:Date}}
                  {where}
                UNION ALL
                SELECT org, namespace, agent, toDate(ts) AS day, operation, {TAIL_STATES}
                FROM loom.agent_events
                WHERE org = {{org:String}}
                  AND ts > (SELECT max(ts_hwm) FROM loom.agent_cost_rollup_daily)
                  AND toDate(ts) BETWEEN {{from_d:Date}} AND {{to_d:Date}}
                  {where}
                GROUP BY {ROLLUP_DIMS}
            )
            {("GROUP BY " + group_by_sql) if group_by_sql else ""}
            ORDER BY ops DESC
            LIMIT 5000
        """
    return list(await ch.query_all(sql, params=params))


async def ask_cache_totals(
    where: str, params: dict[str, Any], *, ch: _CHClient | None = None
) -> tuple[int, float]:
    """(hits, saved USD) of the response cache over the window's memory.ask rows:
    the roll-up path's one extra statement (7 days org-wide over ~500k rows:
    ~10 MB, ~100 ms; a namespace-scoped read is a PK-prefix fraction)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
            SELECT {CACHE_SELECT}
            FROM loom.agent_events
            WHERE org = {{org:String}}
              AND toDate(ts) BETWEEN {{from_d:Date}} AND {{to_d:Date}}
              AND operation = 'memory.ask'
              {where}
        """,
        params=params,
    )
    if not rows:
        return 0, 0.0
    return int(rows[0].get("cache_hits") or 0), float(rows[0].get("cache_saved_usd") or 0.0)


async def namespace_spend_24h(org: str, namespace: str, *, ch: _CHClient | None = None) -> float:
    ch = ch or _default_client()
    row = await ch.query_one(
        """
            SELECT sum(upstream_cost_usd) AS spent
            FROM loom.agent_events
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND ts > now() - INTERVAL 1 DAY
            """,
        params={"org": org, "ns": namespace},
    )
    return float((row or {}).get("spent") or 0.0)


async def namespace_cap(org: str, namespace: str, *, ch: _CHClient | None = None) -> Any:
    """The namespace's own `daily_cost_cap_usd`; None without a namespaces row."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
            SELECT daily_cost_cap_usd
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            """,
        params={"org": org, "ns": namespace},
    )
    return (row or {}).get("daily_cost_cap_usd")


async def org_spend_24h(
    org: str, scope_where: str, scope_params: dict[str, Any], *, ch: _CHClient | None = None
) -> tuple[float, int]:
    """(24 h spend, namespaces that spent) over the caller's scope of the org."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        SELECT sum(upstream_cost_usd) AS spent,
               countDistinct(namespace) AS ns_count
        FROM loom.agent_events
        WHERE org = {{org:String}}
          AND upstream_cost_usd > 0
          AND ts > now() - INTERVAL 1 DAY
          {scope_where}
        """,
        params={"org": org, **scope_params},
    )
    return float((row or {}).get("spent") or 0.0), int((row or {}).get("ns_count") or 0)


async def insert_turn_usage(rows: list[list[Any]], *, ch: Any = None) -> None:
    """Append priced turns to the ledger (ReplacingMergeTree on session + message),
    columns in `TURN_USAGE_COLUMNS` order; async, not awaited server-side."""
    ch = ch or _default_client()
    await ch.client.insert(
        "loom.agent_turn_usage",
        rows,
        column_names=list(TURN_USAGE_COLUMNS),
        settings={"async_insert": 1, "wait_for_async_insert": 0},
    )


async def recorded_turn_times(
    org: str, namespace: str, session_id: str, message_ids: list[str], *, ch: Any = None
) -> dict[str, datetime]:
    """The time each of the session's `message_ids` is recorded with: its
    newest version's, the one a FINAL read sees, whatever has merged."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT message_id, argMax(turn_at, version) AS recorded_at
        FROM loom.agent_turn_usage
        WHERE org = {org:String} AND namespace = {ns:String} AND session_id = {sid:String}
          AND message_id IN {ids:Array(String)}
        GROUP BY message_id
        """,
        params={"org": org, "ns": namespace, "sid": session_id, "ids": message_ids},
    )
    # ClickHouse hands back naive UTC.
    return {r["message_id"]: r["recorded_at"].replace(tzinfo=UTC) for r in rows}


async def agent_summary_rows(
    org: str,
    from_date: date,
    to_date: date,
    *,
    dims: Sequence[str],
    namespace: str | None,
    scope_where: str,
    scope_params: dict[str, Any],
    agent: str | None,
    consumer: str | None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The turn ledger grouped by `dims` (FINAL), at most 5000 rows by cost."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "from_d": from_date, "to_d": to_date}
    where = ""
    if namespace:
        where += " AND namespace = {ns:String}"
        params["ns"] = namespace
    else:
        where += scope_where
        params.update(scope_params)
    if agent is not None:
        where += " AND agent = {agent:String}"
        params["agent"] = agent
    if consumer is not None:
        where += " AND consumer = {consumer:String}"
        params["consumer"] = consumer
    select_dims = ", ".join("toDate(turn_at) AS day" if d == "day" else d for d in dims)
    group_by = ", ".join(dims)
    sql = f"""
        SELECT {select_dims + (", " if select_dims else "")}
               count()                                   AS turns,
               uniqExact(session_id)                     AS sessions,
               sum(toUInt64(input_tokens))               AS input_tokens,
               sum(toUInt64(cache_read_tokens))          AS cache_read_tokens,
               sum(toUInt64(cache_creation_5m_tokens) + toUInt64(cache_creation_1h_tokens))
                                                         AS cache_write_tokens,
               sum(toUInt64(output_tokens))              AS output_tokens,
               sum(cost_usd)                             AS cost_usd,
               countIf(priced = false)                   AS unpriced_turns
        FROM loom.agent_turn_usage FINAL
        WHERE org = {{org:String}}
          AND toDate(turn_at) BETWEEN {{from_d:Date}} AND {{to_d:Date}}
          {where}
        {("GROUP BY " + group_by) if group_by else ""}
        ORDER BY cost_usd DESC
        LIMIT 5000
    """
    return list(await ch.query_all(sql, params=params))
