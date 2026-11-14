"""Repository for /v1/admin/* operator-surface queries.

Functions accept wire-shape parameters directly and return raw CH row
dicts; the handler builds the wire Pydantic shape on top.
"""

from __future__ import annotations

from typing import Any

from server.core.ch_names import SCHEMA, database, logical, telemetry_database
from server.core.naming import not_mirror_sql
from server.core.repo_base import _CHClient, _default_client

# ----------------------------------------------------------------------
# Schema.status — diff migrations against system.tables
# ----------------------------------------------------------------------


async def list_loom_tables(*, ch: _CHClient | None = None) -> list[str]:
    """Names of tables in the schema database, and `loom_telemetry.<name>` for
    the telemetry database (excluding CH MV inner_id bookkeeping). Backs
    /v1/admin/schema.status."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT database, name FROM system.tables WHERE database IN {dbs:Array(String)}",
        {"dbs": [database(), telemetry_database()]},
    )
    return [
        r["name"] if logical(r["database"]) == SCHEMA else f"{logical(r['database'])}.{r['name']}"
        for r in rows
        if not r["name"].startswith(".inner")
    ]


# ----------------------------------------------------------------------
# Audit + events tail
# ----------------------------------------------------------------------


async def audit_summary(
    since_seconds: int,
    *,
    org: str | None = None,
    namespace: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Aggregate audit_log by event_type over a window."""
    ch = ch or _default_client()
    where = [
        "ts >= toDateTime64(now() - INTERVAL {since:UInt32} SECOND, 3, 'UTC')",
        not_mirror_sql(),
    ]
    params: dict[str, Any] = {"since": int(since_seconds)}
    if org:
        where.append("org = {p_org:String}")
        params["p_org"] = org
    if namespace:
        where.append("namespace = {p_ns:String}")
        params["p_ns"] = namespace
    return await ch.query_all(
        f"""
        SELECT event_type,
               count() AS cnt,
               min(ts) AS first_ts,
               max(ts) AS last_ts
        FROM loom.audit_log
        WHERE {" AND ".join(where)}
        GROUP BY event_type
        ORDER BY cnt DESC
        """,
        params=params,
    )


async def setting_changes(
    event_type: str, name: str, limit: int, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The deployment-wide `event_type` rows (org and namespace `-`) of setting
    `name` ("" = all), newest first; the sort key's prefix bounds the read."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT ts, actor, subject
        FROM loom.audit_log
        WHERE org = '-' AND namespace = '-' AND event_type = {event:String}
          AND ({name:String} = '' OR JSONExtractString(subject, 'name') = {name:String})
        ORDER BY ts DESC
        LIMIT {limit:UInt32}
        """,
        params={"event": event_type, "name": name, "limit": int(limit)},
    )


async def events_tail(
    since_seconds: int,
    limit: int,
    *,
    filters: dict[str, str | bool] | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Recent rows from loom.agent_events for live debugging.

    `filters` keys: org, namespace, agent, operation (str), success (bool).
    Unknown keys are ignored — caller validates.
    """
    ch = ch or _default_client()
    where = [
        "ts >= toDateTime64(now() - INTERVAL {since:UInt32} SECOND, 3, 'UTC')",
        not_mirror_sql(),
    ]
    params: dict[str, Any] = {"since": int(since_seconds), "lim": int(limit)}
    filters = filters or {}
    for key in ("org", "namespace", "agent", "operation"):
        val = filters.get(key)
        if val:
            where.append(f"{key} = {{p_{key}:String}}")
            params[f"p_{key}"] = str(val)
    if "success" in filters:
        where.append("success = {p_success:Bool}")
        params["p_success"] = bool(filters["success"])
    return await ch.query_all(
        f"""
        SELECT ts, org, namespace, agent, operation, latency_ms,
               success, error_code, request_id, trace_id
        FROM loom.agent_events
        WHERE {" AND ".join(where)}
        ORDER BY ts DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


# ----------------------------------------------------------------------
# Budget violations
# ----------------------------------------------------------------------


async def budget_violations(
    since_seconds: int,
    limit: int,
    *,
    org: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    ch = ch or _default_client()
    extra_where = ""
    params: dict[str, Any] = {"since": int(since_seconds), "lim": int(limit)}
    if org:
        extra_where += " AND org = {org:String}"
        params["org"] = org
    return await ch.query_all(budget_violations_sql(extra_where), params=params)


def budget_violations_sql(extra_where: str = "") -> str:
    """The `memory.budget_violations` read: budget violations are
    `budget_violation` events on the loom.events stream, the reading in the
    JSON payload, identity typed. Sorting key
    `(org, namespace, kind, ts)`: an org-scoped read is a key-range scan."""
    return f"""
        SELECT namespace,
               JSONExtractString(payload, 'operation')         AS operation,
               toUInt64(sum(1 + JSONExtractUInt(payload, 'suppressed_since_last')))
                                                                AS violations,
               max(JSONExtractFloat(payload, 'overage_ratio'))  AS max_overage_ratio,
               round(avg(JSONExtractFloat(payload, 'overage_ratio')), 3)
                                                                AS avg_overage_ratio,
               max(toDateTime(ts))                              AS last_violation_at
        FROM loom.events
        WHERE kind = 'budget_violation'
          AND ts >= toDateTime64(now() - INTERVAL {{since:UInt32}} SECOND, 3, 'UTC')
          AND {not_mirror_sql()}
          {extra_where}
        GROUP BY namespace, operation
        ORDER BY violations DESC
        LIMIT {{lim:UInt32}}
        """


# The request's consumer: the column, or when it is empty the request's own
# `args.consumer`.
_CONSUMER_KEY = "if(consumer = '', args['consumer'], consumer)"


async def consumers(
    since_seconds: int,
    limit: int,
    *,
    org: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Per-consumer calls, errors, latency and spend over a window.

    Reads the `consumer` column; a row with an empty column falls back to the
    request's own `args.consumer`. Request rows
    only — a worker pass is nobody's consumer. `claimed_calls` /
    `claimed_spend_usd` count the rows whose tag a token claim bound
    (`args['consumer_source'] = 'claimed'`); the rest were the request's own
    word or the subject class."""
    ch = ch or _default_client()
    extra_where = ""
    params: dict[str, Any] = {"since": int(since_seconds), "lim": int(limit)}
    if org:
        extra_where += " AND org = {org:String}"
        params["org"] = org
    consumer_key = _CONSUMER_KEY
    return await ch.query_all(
        f"""
        SELECT {consumer_key}                                    AS consumer_key,
               count()                                          AS calls,
               countIf(NOT success)                             AS errors,
               countIf(NOT success AND error_code = 'INTERNAL_ERROR') AS server_errors,
               round(errors / calls, 4)                         AS error_rate,
               quantileExact(0.5)(latency_ms)                   AS p50_ms,
               quantile(0.95)(latency_ms)                       AS p95_ms,
               round(sum(upstream_cost_usd), 6)                 AS spend_usd,
               countIf(operation = 'code.outcome')              AS outcome_calls,
               uniqExact(namespace)                             AS namespaces,
               uniqExact(operation)                             AS ops_distinct,
               countIf(args['consumer_source'] = 'claimed')     AS claimed_calls,
               round(sumIf(upstream_cost_usd, args['consumer_source'] = 'claimed'), 6)
                                                                AS claimed_spend_usd,
               max(ts)                                          AS last_seen
        FROM loom.agent_events
        WHERE kind = 'request'
          AND ts >= toDateTime64(now() - INTERVAL {{since:UInt32}} SECOND, 3, 'UTC')
          AND {not_mirror_sql()}
          {extra_where}
        GROUP BY consumer_key
        ORDER BY calls DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


# The `consumer` values that are the subject-class fallback, not a tag a
# client sent (server/observability/consumer.py::subject_class).
_UNTAGGED_CONSUMERS = ("", "anonymous", "user", "service-account")


async def consumer_tagging(
    since_seconds: int,
    limit: int,
    *,
    org: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Per (org, agent) — the identity a token's calls are
    written under — how many request rows carried an explicit `consumer` tag
    and how many fell back to the subject class, so a client that tags only
    some of its calls reads as one row with a ratio, not two consumers."""
    ch = ch or _default_client()
    extra_where = ""
    params: dict[str, Any] = {"since": int(since_seconds), "lim": int(limit)}
    if org:
        extra_where += " AND org = {org:String}"
        params["org"] = org
    consumer_key = _CONSUMER_KEY
    return await ch.query_all(
        f"""
        SELECT org, agent,
               count()                                                   AS calls,
               countIf({consumer_key} NOT IN {{untagged:Array(String)}}) AS tagged,
               calls - tagged                                          AS untagged,
               round(tagged / calls, 4)                                AS tagged_ratio,
               anyIf({consumer_key},
                     {consumer_key} NOT IN {{untagged:Array(String)}})  AS tag,
               uniqExactIf(operation,
                           {consumer_key} NOT IN {{untagged:Array(String)}}) AS tagged_ops,
               uniqExactIf(operation,
                           {consumer_key} IN {{untagged:Array(String)}})     AS untagged_ops
        FROM loom.agent_events
        WHERE kind = 'request'
          AND ts >= toDateTime64(now() - INTERVAL {{since:UInt32}} SECOND, 3, 'UTC')
          AND {not_mirror_sql()}
          {extra_where}
        GROUP BY org, agent
        ORDER BY calls DESC
        LIMIT {{lim:UInt32}}
        """,
        params={**params, "untagged": list(_UNTAGGED_CONSUMERS)},
    )


async def consumer_outcomes(
    since_seconds: int,
    limit: int,
    *,
    org: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Outcome feedback per client over a window: the `source` a code.outcome
    row names is the consumer tag by convention (plugin_hook, the shunt, an
    external client's name), so the two sides join by name."""
    ch = ch or _default_client()
    extra_where = ""
    params: dict[str, Any] = {"since": int(since_seconds), "lim": int(limit)}
    if org:
        extra_where += " AND org = {org:String}"
        params["org"] = org
    return await ch.query_all(
        f"""
        SELECT source                                              AS consumer_key,
               count()                                             AS outcome_rows,
               countIf(action IN ('opened_file', 'used_in_answer')) AS outcome_positive,
               countIf(action IN ('fell_back_grep', 'ignored'))     AS outcome_negative,
               countIf(action IN ('shunt_hit', 'shunt_pass'))       AS shunt_rows,
               sum(tokens_saved)                                    AS tokens_saved
        FROM loom.code_search_outcome
        WHERE ts >= toDateTime(now() - INTERVAL {{since:UInt32}} SECOND, 'UTC')
          AND {not_mirror_sql()}
          {extra_where}
        GROUP BY source
        ORDER BY outcome_rows DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


# ----------------------------------------------------------------------
# Fleet observability
# ----------------------------------------------------------------------


async def fleet_cost(
    since_seconds: int,
    top_n: int,
    group_by: str,
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Top-N rollup over agent_events. group_by ∈ {namespace, agent, org}."""
    ch = ch or _default_client()
    namespaces_select = (
        ", uniqExact(namespace) AS namespaces_distinct" if group_by != "namespace" else ""
    )
    return await ch.query_all(
        f"""
        SELECT
            {group_by}            AS key,
            sum(upstream_cost_usd) AS total_cost_usd,
            count()                AS total_calls,
            uniqExact(operation)   AS ops_distinct
            {namespaces_select}
        FROM loom.agent_events
        WHERE ts >= now() - INTERVAL {{since:UInt32}} SECOND AND {not_mirror_sql()}
        GROUP BY {group_by}
        ORDER BY total_cost_usd DESC
        LIMIT {{top_n:UInt32}}
        """,
        params={"since": int(since_seconds), "top_n": int(top_n)},
    )
