"""The owner's view of their memory.

The owner of a memory should be able to see how it is stored, who reads it and what it is
worth. Loom already has every table that answers this — `loom.memory` (rows carry
`user_id`), `loom.memory_exposures` (one row per read, the returned keys, the token and
agent that read them) and `loom.memory_usage_totals` (recall / used / harm counts per
memory) — but they are keyed for an operator. This module turns them the other way round:
given a user, what do they own, who read it, how was it used.

Pure SQL over existing tables; no new column, no model call. The caller's
identity is bound by `identity/domain/read_scope` before this runs, so `user_id`
here is already the owner the token may see.
"""

from __future__ import annotations

from typing import Any

from server.auth.context import OrgScopeFilter

_ROWS_SQL = """
SELECT namespace, memory_type, count() AS n
FROM loom.memory FINAL
WHERE org = {org:String} AND user_id = {uid:String}
  AND status = 'active' AND tombstoned_at IS NULL__NS__
GROUP BY namespace, memory_type
ORDER BY namespace, memory_type
"""

# `namespace` in the outer WHERE is `e.namespace`, by the SELECT alias, so one
# scope predicate serves the ledger and the `loom.memory` subquery alike.
_READS_SQL = """
SELECT e.namespace AS namespace, e.agent AS agent, e.actor AS actor,
       count() AS calls, uniq(k) AS distinct_keys, max(e.ts) AS last_read
FROM loom.memory_exposures AS e
ARRAY JOIN e.returned_keys AS k
WHERE e.org = {org:String}
  AND e.ts > now() - INTERVAL {hours:UInt32} HOUR__NS__
  AND (e.namespace, k) IN (
      SELECT namespace, memory_key FROM loom.memory FINAL
      WHERE org = {org:String} AND user_id = {uid:String}
        AND status = 'active' AND tombstoned_at IS NULL__NS__
  )
GROUP BY e.namespace, e.agent, e.actor
ORDER BY calls DESC, distinct_keys DESC
LIMIT {lim:UInt32}
"""

_USAGE_SQL = """
SELECT m.namespace AS namespace,
       sum(u.recall_count) AS recalls, sum(u.used_count) AS used,
       sum(u.harm_count) AS harm, max(u.last_recalled_at) AS last_recalled
FROM loom.memory_usage_totals AS u
INNER JOIN (
    SELECT memory_id, namespace FROM loom.memory FINAL
    WHERE org = {org:String} AND user_id = {uid:String}
      AND status = 'active' AND tombstoned_at IS NULL__NS__
) AS m ON u.memory_id = m.memory_id
GROUP BY m.namespace
ORDER BY m.namespace
"""


def namespace_scope(namespaces: list[str]) -> OrgScopeFilter:
    """The predicate bounding the report to `namespaces`; an empty list is an
    inaccessible scope, never an unbounded one."""
    return OrgScopeFilter(
        accessible=bool(namespaces),
        where=" AND namespace IN {nss:Array(String)}",
        params={"nss": sorted(set(namespaces))},
    )


def build_queries(
    *, org: str, user_id: str, scope: OrgScopeFilter, since_hours: int, limit: int
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """The three statements and their shared parameters. Pure."""
    params: dict[str, Any] = {
        "org": org,
        "uid": user_id,
        "hours": max(1, int(since_hours)),
        "lim": max(1, int(limit)),
        **scope.params,
    }
    ns = scope.where
    return (
        [
            (_ROWS_SQL.replace("__NS__", ns), params),
            (_READS_SQL.replace("__NS__", ns), params),
            (_USAGE_SQL.replace("__NS__", ns), params),
        ],
        params,
    )


async def owner_report(
    *,
    org: str,
    user_id: str,
    scope: OrgScopeFilter,
    since_hours: int = 720,
    limit: int = 50,
    ch: Any | None = None,
) -> dict[str, Any]:
    """{rows: [...], reads: [...], usage: [...]} for one owner. Each list is a
    list of plain dicts; empty lists when the user owns nothing, and no
    statement runs when `scope` reaches no namespace."""
    from server.core.ch import get_client

    if not scope.accessible:
        return {"rows": [], "reads": [], "usage": []}
    ch = ch or get_client()
    (rows_q, reads_q, usage_q), _ = build_queries(
        org=org, user_id=user_id, scope=scope, since_hours=since_hours, limit=limit
    )
    rows = [dict(r) for r in await ch.query_all(rows_q[0], rows_q[1])]
    reads = [dict(r) for r in await ch.query_all(reads_q[0], reads_q[1])]
    usage = [dict(r) for r in await ch.query_all(usage_q[0], usage_q[1])]
    return {"rows": rows, "reads": reads, "usage": usage}


__all__ = ["build_queries", "namespace_scope", "owner_report"]
