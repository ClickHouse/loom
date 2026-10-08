"""Erasure invariants read outside a saga: the entity-graph and entity-vector
orphans the state probe and the re-verify CLI count, and whether the OTel
metrics tables carry the index the ClickStack leg needs."""

from __future__ import annotations

from typing import Any, cast

from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.rows import (
    OrphanGraphRow,
)
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

# The ClickStack leg matches OTel rows by a Map value; without a
# `mapValues(Attributes)` bloom filter every count over a metrics table is a
# full scan. The collector
# ships that index on logs and traces, not on metrics.
OTEL_METRICS_TABLES: tuple[str, ...] = (
    "otel_metrics_gauge",
    "otel_metrics_sum",
    "otel_metrics_histogram",
    "otel_metrics_exponential_histogram",
    "otel_metrics_summary",
)
OTEL_INDEX_SQL = (
    "SELECT t.name AS name, countIf(i.expr = 'mapValues(Attributes)') AS n "
    "FROM system.tables AS t LEFT JOIN system.data_skipping_indices AS i "
    "ON i.database = t.database AND i.table = t.name "
    "WHERE t.database = {db:String} AND t.name IN {tables:Array(String)} "
    "GROUP BY name ORDER BY name"
)
_OTEL_INDEX_TTL_S = 3600.0
_OTEL_INDEX_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}


async def otel_metrics_unindexed(
    database: str, *, ch: _CHClient | None = None, now: float | None = None
) -> tuple[str, ...]:
    """The OTel metrics tables present in `database` without a
    `mapValues(Attributes)` skip index — what the health `problems` list and
    `loom doctor` report. One statement, cached an hour per process: the
    health endpoint is polled, the schema is not."""
    import time as _time

    t = _time.monotonic() if now is None else now
    hit = _OTEL_INDEX_CACHE.get(database)
    if hit is not None and t - hit[0] < _OTEL_INDEX_TTL_S:
        return hit[1]
    ch = ch or _default_client()
    rows = await ch.query_all(
        OTEL_INDEX_SQL, params={"db": database, "tables": list(OTEL_METRICS_TABLES)}
    )
    out = tuple(sorted(str(r["name"]) for r in rows if not int(r.get("n") or 0)))
    _OTEL_INDEX_CACHE[database] = (t, out)
    return out


# Two invariants the state probe and the re-verify CLI share.
#
# A namespace whose entity graph outlived everything that could have written
# it: edges present, no live memory row and no live knowledge chunk (the
# chunk entity extractor writes edges from documents) — the residue of a saga
# that failed before `entity_graph.delete`.
ORPHAN_GRAPH_NAMESPACES_SQL = (
    """
SELECT g.org AS org, g.namespace AS namespace, g.edges AS edges
FROM (
    SELECT org, namespace, count() AS edges
    FROM loom.entity_edges
    GROUP BY org, namespace
) AS g
LEFT ANTI JOIN (
    SELECT org, namespace FROM loom.memory FINAL
    WHERE tombstoned_at IS NULL
    GROUP BY org, namespace
    UNION ALL
    SELECT org, namespace FROM loom.chunks
    WHERE """
    + LIVE_CHUNK_SQL
    + """
    GROUP BY org, namespace
) AS m ON g.org = m.org AND g.namespace = m.namespace
"""
)

# An entity vector whose memory is gone: the entity boost can still rank the
# erased memory's key from its entity text. Keyed on the identity the table
# carries (no session_id on entity_vectors).
ORPHAN_ENTITY_VECTORS_SQL = """
SELECT e.org AS org, e.namespace AS namespace, e.memory_key AS memory_key
FROM (
    SELECT org, namespace, agent, user_id, memory_key
    FROM loom.entity_vectors FINAL
    WHERE tombstoned_at IS NULL
) AS e
LEFT ANTI JOIN (
    SELECT org, namespace, agent, user_id, memory_key
    FROM loom.memory FINAL
    WHERE tombstoned_at IS NULL
) AS m ON e.org = m.org AND e.namespace = m.namespace
     AND e.agent = m.agent AND e.user_id = m.user_id
     AND e.memory_key = m.memory_key
"""


async def orphan_graph_namespaces(
    *, org: str | None = None, ch: _CHClient | None = None
) -> list[OrphanGraphRow]:
    """(org, namespace, edges) of every namespace with graph rows and nothing
    live that wrote them; largest first."""
    ch = ch or _default_client()
    where = "WHERE org = {org:String}" if org else ""
    params: dict[str, Any] = {"org": org} if org else {}
    rows = await ch.query_all(
        f"SELECT org, namespace, edges FROM ({ORPHAN_GRAPH_NAMESPACES_SQL}) {where} "
        "ORDER BY edges DESC, namespace",
        params=params,
    )
    return cast("list[OrphanGraphRow]", rows)


async def integrity_counts(*, ch: _CHClient | None = None) -> dict[str, int]:
    """The two erasure-invariant gauges as one read, for the CLI's closing line."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT (SELECT count() FROM ({ORPHAN_ENTITY_VECTORS_SQL})) AS orphan_entity_vectors, "
        f"(SELECT count() FROM ({ORPHAN_GRAPH_NAMESPACES_SQL})) AS orphan_graph_namespaces"
    )
    return {
        "orphan_entity_vectors": int((row or {}).get("orphan_entity_vectors") or 0),
        "orphan_graph_namespaces": int((row or {}).get("orphan_graph_namespaces") or 0),
    }
