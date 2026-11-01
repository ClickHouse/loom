"""The statements behind `namespace.inventory`: one count and newest write per plane,
and the identities observed on live memory rows. Every read of rows that carry a user
takes the request's read scope."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and

# Plane → (table, live predicate, newest-write column). Each table carries
# `user_id` and `session_id`, so the read scope applies to all of them. A rewritten
# record keeps its earlier version as a `superseded` row; the record reads serve
# `active` rows only.
_SCOPED_PLANES: dict[str, tuple[str, str, str]] = {
    "memories": ("loom.memory", "tombstoned_at IS NULL", "updated_at"),
    "records": ("loom.memory_records", "status = 'active' AND tombstoned_at IS NULL", "updated_at"),
    "runs": ("loom.agent_runs", "1", "started_at"),
}

# Observed population → the memory column that names it. `user_id` '-' is the unset
# identity, never a user; `actor` '' is a row written before it was stamped.
_OBSERVED: dict[str, tuple[str, str]] = {
    "agents": ("agent", "agent != ''"),
    "authors": ("actor", "actor != ''"),
    "subjects": ("user_id", "user_id NOT IN ('', '-')"),
}


async def plane_count(
    org: str, namespace: str, plane: str, *, scope: ReadFilters, ch: _CHClient | None = None
) -> tuple[int, datetime | None]:
    """Live items of `plane` inside `scope`, and the newest write among them."""
    ch = ch or _default_client()
    table, live, at = _SCOPED_PLANES[plane]
    params: dict[str, Any] = {"org": org, "ns": namespace}
    row = await ch.query_one(
        f"SELECT count() AS n, max({at}) AS latest FROM {table} FINAL "
        f"WHERE org = {{org:String}} AND namespace = {{ns:String}} AND {live}"
        + scope_and(scope, params),
        params,
    )
    return _count(row)


async def document_count(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> tuple[int, datetime | None]:
    """Current documents of the namespace (superseded versions left out)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT count() AS n, max(ingested_at) AS latest FROM loom.documents FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND superseded_at IS NULL",
        {"org": org, "ns": namespace},
    )
    return _count(row)


async def webhook_count(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> tuple[int, datetime | None]:
    """Registered webhooks of the namespace (removed ones left out)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT count() AS n, max(created_at) AS latest FROM loom.memory_webhooks FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND is_deleted = 0",
        {"org": org, "ns": namespace},
    )
    return _count(row)


async def observed_identities(
    org: str,
    namespace: str,
    population: str,
    *,
    scope: ReadFilters,
    limit: int,
    ch: _CHClient | None = None,
) -> tuple[int, list[tuple[str, int]]]:
    """Distinct values of the population's column on live memory rows inside `scope`,
    and the `limit` that carry the most rows."""
    ch = ch or _default_client()
    col, named = _OBSERVED[population]
    # The total rides on the listed rows, so at least one is read.
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": max(1, limit)}
    rows = await ch.query_all(
        f"SELECT {col} AS name, count() AS n, count() OVER () AS total "
        "FROM loom.memory FINAL "
        f"WHERE org = {{org:String}} AND namespace = {{ns:String}} AND tombstoned_at IS NULL "
        f"AND {named}{scope_and(scope, params)} "
        f"GROUP BY {col} ORDER BY n DESC, name LIMIT {{lim:UInt32}}",
        params,
    )
    total = int(rows[0]["total"]) if rows else 0
    return total, [(str(r["name"]), int(r["n"])) for r in rows][:limit]


def _count(row: dict[str, Any] | None) -> tuple[int, datetime | None]:
    n = int(row["n"]) if row else 0
    # max() over no rows is the epoch, not a write.
    return n, (row["latest"] if row and n else None)
