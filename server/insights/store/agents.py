"""The agents plane's statements: who called into an org or namespace over a
window of `agent_events`, and when each was first seen."""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client


def connected_where(
    org: str,
    window_hours: int,
    *,
    namespace: str | None,
    scope_where: str,
    scope_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """The window's predicate: the org, the window, real agents only (the sentinel
    names "-", "*" and "" are left out), then the named namespace or the caller's
    scope."""
    where = "org = {org:String} AND ts > now() - toIntervalHour({wh:UInt32}) AND agent NOT IN ('-', '*', '')"  # noqa: E501
    params: dict[str, Any] = {"org": org, "wh": int(window_hours)}
    if namespace:
        where += " AND namespace = {ns:String}"
        params["ns"] = namespace
    else:
        where += scope_where
        params.update(scope_params)
    return where, params


async def new_agent_count(
    where: str, params: dict[str, Any], *, lookback_days: int, ch: _CHClient | None = None
) -> int:
    """Agents whose first observation (inside the lookback) is inside the window and
    who did something in it: the badge's number in one aggregate, no rows, no join."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
            SELECT count() AS n
            FROM (
                SELECT agent,
                       min(ts) AS first_seen,
                       countIf(ts > now() - toIntervalHour({{wh:UInt32}})) AS ops
                FROM loom.agent_events
                WHERE {where.replace("ts > now() - toIntervalHour({wh:UInt32}) AND ", "")}
                  AND ts > now() - toIntervalDay({{lb:UInt16}})
                GROUP BY agent
            )
            WHERE first_seen > now() - toIntervalHour({{wh:UInt32}}) AND ops > 0
            """,
        params={**params, "lb": int(lookback_days)},
    )
    return int((row or {}).get("n") or 0)


async def connected_rows(
    where: str, params: dict[str, Any], *, lookback_days: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Per agent in the window: last seen, ops, namespaces touched, the latest
    namespace, cost, and `first_seen` from the `all_time` CTE. That CTE is bounded
    to `lookback_days`: unbounded, it would scan the org's whole event history."""
    ch = ch or _default_client()
    return list(
        await ch.query_all(
            f"""
        WITH all_time AS (
            SELECT agent, min(ts) AS first_seen
            FROM loom.agent_events
            WHERE org = {{org:String}} AND agent NOT IN ('-', '*', '')
              AND ts > now() - toIntervalDay({{lb:UInt16}})
            GROUP BY agent
        )
        SELECT
            w.agent AS agent,
            w.last_seen AS last_seen,
            w.ops AS ops,
            w.namespaces_touched AS namespaces_touched,
            w.primary_namespace AS primary_namespace,
            w.cost_usd AS cost_usd,
            at.first_seen AS first_seen
        FROM (
            SELECT
                agent,
                max(ts) AS last_seen,
                count() AS ops,
                countDistinct(namespace) AS namespaces_touched,
                argMax(namespace, ts) AS primary_namespace,
                sum(upstream_cost_usd) AS cost_usd
            FROM loom.agent_events
            WHERE {where}
            GROUP BY agent
        ) AS w
        LEFT JOIN all_time AS at ON w.agent = at.agent
        ORDER BY w.last_seen DESC
        """,
            params={**params, "lb": int(lookback_days)},
        )
    )
