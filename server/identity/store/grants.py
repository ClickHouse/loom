"""Repository for loom.namespace_grants.

Per-namespace authz grants. ReplacingMergeTree(version) collapses
on (org, namespace, subject) so a new grant supersedes the older
one; revocation is a separate INSERT with `revoked_at` populated
+ a higher version.
"""

from __future__ import annotations

from typing import Any

from server.core import grant_cache
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT
from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.grants import GrantState, PendingGrant


async def insert_row(
    row: dict[str, Any],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert one grant row. Used by both create and revoke (revoke is
    a new row with `revoked_at` populated + bumped version)."""
    ch = ch or _default_client()
    try:
        await ch.insert(
            "loom.namespace_grants",
            [row],
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
    finally:
        _invalidate([row])  # a reply lost after the write landed still drops it


async def insert_rows(
    rows: list[dict[str, Any]],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert several grant rows in ONE synchronous statement. A mint that
    grants a token eleven restricted namespaces paid eleven inserts at
    1.6 s p50 / 5 s p95 each on a Cloud cluster; one statement costs one."""
    if not rows:
        return
    ch = ch or _default_client()
    try:
        await ch.insert(
            "loom.namespace_grants",
            rows,
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
    finally:
        _invalidate(rows)


def _invalidate(rows: list[dict[str, Any]]) -> None:
    """This server's namespace gate sees a grant change at once (its other web
    processes through the write count `grant_cache` keeps); others within
    `grant_cache_ttl_seconds`."""
    for org in {r["org"] for r in rows}:
        grant_cache.invalidate(org, [r["subject"] for r in rows if r["org"] == org])


async def pending_for_admins(
    org: str,
    subjects: list[str],
    *,
    limit: int = 500,
    ch: _CHClient | None = None,
) -> list[PendingGrant]:
    """Every pending grant (a join request, or an elevation request on an
    existing grant) in the namespaces where any of `subjects` — one person's
    spellings — holds an active admin grant, excluding the person's own
    requests. One statement for the approvals bell, instead of one
    `grants.list` per administered namespace every 30 s."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT p.namespace AS namespace,
               ifNull(n.kind, 'memory') AS kind,
               p.subject AS subject,
               p.role AS role,
               p.status AS status,
               p.pending_role AS pending_role,
               p.pending_since AS pending_since,
               p.granted_at AS granted_at
        FROM (
            SELECT namespace, subject, role,
                   ifNull(toString(status), 'active') AS status,
                   pending_role, pending_since, granted_at
            FROM loom.namespace_grants FINAL
            WHERE org = {org:String}
              AND revoked_at IS NULL
              AND (toString(status) = 'pending' OR pending_role IS NOT NULL)
              AND subject NOT IN {subs:Array(String)}
              AND namespace IN (
                  SELECT namespace FROM loom.namespace_grants FINAL
                  WHERE org = {org:String} AND subject IN {subs:Array(String)}
                    AND role = 'admin' AND {live}
              )
        ) AS p
        LEFT JOIN (
            SELECT namespace, kind FROM loom.namespaces FINAL WHERE org = {org:String}
        ) AS n ON n.namespace = p.namespace
        ORDER BY pending_since DESC, granted_at DESC
        LIMIT {lim:UInt32}
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "subs": subjects, "lim": int(limit)},
    )
    return [PendingGrant.from_row(r) for r in rows]


async def list_active(
    org: str,
    namespace: str,
    *,
    limit: int = 100,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Grants on (org, namespace) — revoked rows excluded. Includes BOTH
    active and pending grants; callers filter by `status` field if they
    only want one or the other."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT subject, role, status, pending_role, pending_since,
               granted_at, granted_by
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND revoked_at IS NULL
        ORDER BY granted_at DESC
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )


async def find_active_grant(
    org: str,
    namespace: str,
    subject: str,
    *,
    ch: _CHClient | None = None,
) -> GrantState | None:
    """Look up the current ACTIVE grant for (org, ns, subject), if any.
    Pending grants are intentionally excluded — they don't authorize
    action yet."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT role, granted_by, status, pending_role, pending_since
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND subject = {sub:String} AND {live}
        LIMIT 1
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "ns": namespace, "sub": subject},
    )
    return None if row is None else GrantState.from_row(row)


async def find_any_grant(
    org: str,
    namespace: str,
    subject: str,
    *,
    ch: _CHClient | None = None,
) -> GrantState | None:
    """Look up ANY non-revoked grant — active OR pending. Used by the
    join handler to detect already-requested-but-not-yet-approved
    self-joins so they aren't duplicated."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT role, granted_by, status, pending_role, pending_since
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND subject = {sub:String} AND revoked_at IS NULL
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "sub": subject},
    )
    return None if row is None else GrantState.from_row(row)


async def count_active_admins(
    org: str,
    namespace: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Live admin grants on (org, namespace). A pending admin request is not an admin: it
    neither keeps a namespace owned nor blocks an operator adopting an orphaned one."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT count() AS n FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND role = 'admin' AND {live}
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "ns": namespace},
    )
    return int((row or {}).get("n") or 0)


async def best_live_role(
    org: str, namespace: str, subjects: list[str], *, ch: _CHClient | None = None
) -> str:
    """The highest live grant role any of `subjects` holds on (org, namespace),
    "" when none."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT g.role AS role
        FROM (SELECT * FROM loom.namespace_grants FINAL) AS g
        WHERE g.org = {org:String}
          AND g.subject IN {subs:Array(String)}
          AND g.namespace = {ns:String}
          AND {live}
        ORDER BY {rank} DESC
        LIMIT 1
        """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
        params={"org": org, "subs": subjects, "ns": namespace},
    )
    return str((row or {}).get("role") or "")


async def ranked_live_grants(
    org: str, subjects: list[str], *, limit: int = 200, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """`subjects`' live grants in `org` (namespace, role), highest role
    first, then the most recent."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT namespace, role
        FROM (SELECT * FROM loom.namespace_grants FINAL) AS g
        WHERE g.org = {org:String}
          AND g.subject IN {subs:Array(String)}
          AND {live}
        ORDER BY {rank} DESC, g.granted_at DESC
        LIMIT {lim:UInt32}
        """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
        params={"org": org, "subs": subjects, "lim": int(limit)},
    )
    return list(rows or [])
