"""Namespace registry census — who holds live memory without an owner row,
and which registered bench namespaces have gone idle.

The SQL lives here so the two gauges, the `namespaces-gc` worker and the
`namespaces-adopt` CLI read the same statements; a gauge that reads one
shape while the operator's command reads another cannot agree on zero.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.census import BenchGcCandidate, UnregisteredNamespace

# Every (org, namespace) holding a live memory row and no loom.namespaces row
# of any version. The registry is small (about a thousand rows); the memory
# side is one GROUP BY over the live rows (0.1 s at 830k rows).
UNREGISTERED_LIVE_SQL = """
SELECT m.org AS org, m.namespace AS namespace, count() AS live_rows,
       max(m.created_at) AS last_write
FROM loom.memory AS m FINAL
LEFT ANTI JOIN (SELECT org, namespace FROM loom.namespaces) AS n
  ON m.org = n.org AND m.namespace = n.namespace
WHERE m.tombstoned_at IS NULL
GROUP BY m.org, m.namespace
"""

# Registered `kind = 'bench'` namespaces that still hold live memory and have
# not been written to for {idle:UInt32} hours. The write mark
# is the primary clock; a namespace with no mark falls
# back to its newest memory row. The registry row itself must be older than
# the idle window too, so a namespace registered ahead of its first write is
# never a candidate.
BENCH_GC_CANDIDATES_SQL = """
SELECT n.org AS org, n.namespace AS namespace, n.owner_email AS owner_email,
       live.live_rows AS live_rows,
       greatest(ifNull(w.last_write, toDateTime64(0, 3)),
                toDateTime64(live.last_created, 3)) AS last_write
FROM (SELECT org, namespace, owner_email, created_at
      FROM loom.namespaces FINAL
      WHERE kind = 'bench' AND created_at < now() - INTERVAL {idle:UInt32} HOUR) AS n
INNER JOIN (SELECT org, namespace, count() AS live_rows, max(created_at) AS last_created
            FROM loom.memory FINAL
            WHERE tombstoned_at IS NULL
            GROUP BY org, namespace) AS live
  ON n.org = live.org AND n.namespace = live.namespace
LEFT JOIN (SELECT org, namespace, max(last_write) AS last_write
           FROM loom.namespace_write_marks
           GROUP BY org, namespace) AS w
  ON n.org = w.org AND n.namespace = w.namespace
WHERE greatest(ifNull(w.last_write, toDateTime64(0, 3)),
               toDateTime64(live.last_created, 3)) < now64(3) - INTERVAL {idle:UInt32} HOUR
"""


async def unregistered_live(
    *, org: str | None = None, ch: _CHClient | None = None
) -> list[UnregisteredNamespace]:
    """One row per unregistered live namespace, largest first."""
    ch = ch or _default_client()
    where = "WHERE org = {org:String}" if org else ""
    params = {"org": org} if org else {}
    rows = await ch.query_all(
        f"SELECT org, namespace, live_rows, last_write FROM ({UNREGISTERED_LIVE_SQL}) "
        f"{where} ORDER BY live_rows DESC, namespace",
        params,
    )
    return [UnregisteredNamespace.from_row(r) for r in rows]


async def bench_gc_candidates(
    *,
    idle_hours: int,
    org: str | None = None,
    namespaces: tuple[str, ...] = (),
    limit: int = 0,
    ch: _CHClient | None = None,
) -> list[BenchGcCandidate]:
    """Registered bench namespaces idle past `idle_hours` that still hold
    live memory, oldest write first. `org` / `namespaces` narrow the scan
    (the CLI's one-off run); `limit` bounds a pass."""
    ch = ch or _default_client()
    preds: list[str] = []
    params: dict[str, Any] = {"idle": int(idle_hours)}
    if org:
        preds.append("org = {org:String}")
        params["org"] = org
    if namespaces:
        preds.append("namespace IN {nss:Array(String)}")
        params["nss"] = list(namespaces)
    where = f"WHERE {' AND '.join(preds)}" if preds else ""
    lim = ""
    if limit and limit > 0:
        lim = "LIMIT {lim:UInt32}"
        params["lim"] = int(limit)
    rows = await ch.query_all(
        f"SELECT org, namespace, owner_email, live_rows, last_write "
        f"FROM ({BENCH_GC_CANDIDATES_SQL}) {where} ORDER BY last_write ASC, namespace {lim}",
        params,
    )
    return [BenchGcCandidate.from_row(r) for r in rows]
