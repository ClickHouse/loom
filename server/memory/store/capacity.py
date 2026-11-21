"""Bounded, opt-in capacity observations; no diagnostic creates a counter or row."""

from __future__ import annotations

import math
from typing import Any

from server.core import pg
from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and
from server.memory.domain.temporal_sql import SERVED_STATUSES
from server.memory.store import pg_memory
from server.memory.store.reads import _SERVED_ROWS
from server.settings import settings


def _ch_bounds() -> str:
    """The operator's diagnostic ceilings, applied to every bounded read."""
    return (
        f" SETTINGS max_execution_time = {settings.capacity_read_max_execution_s:g}, "
        f"max_rows_to_read = {settings.capacity_scope_max_rows}, "
        f"max_bytes_to_read = {settings.capacity_read_max_bytes}, "
        f"max_memory_usage = {settings.capacity_read_max_bytes}, "
        "read_overflow_mode = 'throw', timeout_overflow_mode = 'throw', max_threads = 1"
    )


# Successful requests, not versions or committed rows. Bulk is returned separately.
WRITE_OPERATIONS = (
    "memory.set",
    "memory.update_by_id",
    "memory.delete",
    "memory.pin",
    "memory.unpin",
    "memory.bulk_set",
)


async def write_requests(*, ch: _CHClient | None = None) -> dict[str, Any] | None:
    """One complete five-minute window ending at the rollup's ingested watermark."""
    return await (ch or _default_client()).query_one(
        """
        WITH (
            SELECT maxOrNull(ingested_hwm) FROM loom_telemetry.sli_minute
            WHERE slo_class = '' AND minute >= toStartOfMinute(now() - INTERVAL 31 DAY)
        ) AS mark, toStartOfMinute(mark) AS until
        SELECT mark AS ingested_hwm, now64(3) AS observed_at,
               until - INTERVAL 5 MINUTE AS window_start, until AS window_end,
               sumIf(requests - least(requests, server_errors + client_errors),
                     operation != 'memory.bulk_set') AS single_requests,
               sumIf(requests - least(requests, server_errors + client_errors),
                     operation = 'memory.bulk_set') AS bulk_requests
        FROM loom_telemetry.sli_minute
        WHERE slo_class != '' AND minute >= until - INTERVAL 5 MINUTE AND minute < until
          AND operation IN {operations:Array(String)} AND ingested_hwm <= mark
        """
        + _ch_bounds(),
        {"operations": list(WRITE_OPERATIONS)},
    )


async def live_heads(
    org: str, namespace: str, *, scope: ReadFilters, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """Current served heads, with the same status/tombstone predicates as inventory."""
    if pg.enabled():
        args: list[Any] = [
            org,
            namespace,
            settings.capacity_scope_max_rows + 1,
            list(SERVED_STATUSES),
        ]
        where = pg_memory._scope_sql(scope, args)
        # A positive native deadline rounds up to at least PostgreSQL's 1 ms unit.
        statement_ms = math.ceil(settings.capacity_read_max_execution_s * 1000)
        async with pg.dedicated(statement_timeout_ms=statement_ms) as conn, conn.transaction():
            await conn.execute("SET LOCAL transaction_read_only = on")
            await conn.execute(f"SET LOCAL work_mem = '{settings.capacity_pg_work_mem_mib}MB'")
            row = await conn.fetchrow(
                "SELECT count(*) AS n, statement_timestamp() AS observed_at FROM ("
                "SELECT 1 FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
                "AND row->>'tombstoned_at' IS NULL AND row->>'status' = ANY($4::text[])"
                + where
                + " LIMIT $3) AS heads",
                *args,
            )
            return dict(row) if row is not None else None
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "cap": settings.capacity_scope_max_rows + 1,
    }
    return await (ch or _default_client()).query_one(
        "SELECT count() AS n, now64(3) AS observed_at FROM (SELECT 1 FROM loom.memory FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND "
        + _SERVED_ROWS
        + scope_and(scope, params)
        + " LIMIT {cap:UInt32})"
        + _ch_bounds(),
        params,
    )


async def vector_rows(
    org: str, namespace: str, *, scope: ReadFilters, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """Physical memory-vector rows, including versions/tombstones; not served heads."""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "cap": settings.capacity_scope_max_rows + 1,
    }
    return await (ch or _default_client()).query_one(
        "SELECT count() AS n, now64(3) AS observed_at FROM ("
        "SELECT 1 FROM loom.memory_embeddings WHERE org = {org:String} AND namespace = {ns:String}"
        + scope_and(scope, params)
        + " LIMIT {cap:UInt32})"
        + _ch_bounds(),
        params,
    )
