"""The reclaim worker's statements: how many rows a purge table's active parts
hold against how many a read sees, and the deleted-mask rewrite."""

from __future__ import annotations

from typing import Any

_PARTS_SQL = """
SELECT sum(rows) AS on_disk, sum(bytes_on_disk) AS bytes,
       countIf(has_lightweight_delete) AS masked_parts
FROM system.parts
WHERE database = {db:String} AND table = {t:String} AND active
"""
_PENDING_SQL = """
SELECT count() AS n FROM system.mutations
WHERE database = {db:String} AND table = {t:String} AND is_done = 0
"""


async def masked_rows(ch: Any, table: str) -> tuple[int, int, int] | None:
    """(rows on disk, rows a read sees, bytes on disk) of a table with masked
    parts; None when nothing is masked or a mutation still runs there."""
    db, name = table.split(".", 1)
    parts = await ch.query_one(_PARTS_SQL, {"db": db, "t": name})
    if not parts or not int(parts.get("masked_parts") or 0):
        return None
    if int((await ch.query_one(_PENDING_SQL, {"db": db, "t": name}) or {}).get("n") or 0):
        return None  # a purge or an earlier reclaim is still running there
    visible = await ch.query_one(f"SELECT count() AS n FROM {table}")
    return (
        int(parts.get("on_disk") or 0),
        int((visible or {}).get("n") or 0),
        int(parts.get("bytes") or 0),
    )


async def apply_deleted_mask(ch: Any, table: str) -> None:
    """Rewrite the table's masked parts without their purged rows, in the background."""
    await ch.execute(f"ALTER TABLE {table} APPLY DELETED MASK SETTINGS mutations_sync = 0")
