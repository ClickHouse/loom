"""The compaction statements over one allowlisted code vector table: its active parts
and physical rows, its distinct keys, the merges running on it, and the
`OPTIMIZE ... FINAL` that collapses its copies. The caller resolves the table name
against the allowlist first (it is interpolated into SQL)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The code vector tables' ReplacingMergeTree key.
_KEY = "org, namespace, embedding_model, path, chunk_id"


def _split(table: str) -> tuple[str, str]:
    db, _, name = table.partition(".")
    return db, name


async def active_parts(ch: Any, table: str) -> tuple[int, int]:
    """(active parts, physical rows) from `system.parts`: metadata only."""
    db, name = _split(table)
    row = await ch.query_one(
        "SELECT count() AS parts, sum(rows) AS rows FROM system.parts "
        "WHERE database = {db:String} AND table = {t:String} AND active",
        {"db": db, "t": name},
    )
    if not row:
        return 0, 0
    return int(row.get("parts") or 0), int(row.get("rows") or 0)


async def distinct_keys(ch: Any, table: str) -> int:
    """`uniq` of the key tuple: reads the five key columns, no vectors."""
    row = await ch.query_one(f"SELECT uniq({_KEY}) AS keys FROM {table}")
    return int(row.get("keys") or 0) if row else 0


async def merges_running(ch: Any, table: str) -> int:
    """Merges of `table` running on any replica (this node's alone without a cluster)."""
    db, name = _split(table)
    params = {"db": db, "t": name}
    where = "WHERE database = {db:String} AND table = {t:String}"
    try:
        row = await ch.query_one(
            f"SELECT count() AS n FROM clusterAllReplicas('default', system.merges) {where}",
            params,
        )
    except Exception as e:  # noqa: BLE001 — a single OSS node has no `default` cluster
        logger.info("compaction: clusterAllReplicas unavailable (%s); local merges", str(e)[:120])
        row = await ch.query_one(f"SELECT count() AS n FROM system.merges {where}", params)
    return int(row.get("n") or 0) if row else 0


async def optimize_final(ch: Any, table: str) -> None:
    """Start the merge that collapses `table`'s copies; on SharedMergeTree it
    returns before the merge lands."""
    await ch.execute(f"OPTIMIZE TABLE {table} FINAL")
