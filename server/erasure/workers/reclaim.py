"""Erasure reclaim worker: the bytes of purged rows leave the disk.

A purge of a wide table is a lightweight `DELETE FROM`
(`LIGHTWEIGHT_PURGE_TABLES`): the rows leave every read at once through the
`_row_exists` mask, and their bytes leave at the part's next merge. Large,
settled parts are rarely picked for a merge, so on a deployment that erases
bench namespaces often the masked rows stay: purged rows can be most of the
rows on disk of `memory_embeddings`, `memory_chunks` and `entity_vectors`.

Each pass reads, per purge table, the rows its active parts hold and the rows
a read sees; a table whose dead share and dead bytes both pass the thresholds
gets one `ALTER TABLE … APPLY DELETED MASK`, which rewrites only its masked
parts without those rows. One table per pass, the one with the most dead
bytes, and never a table that already has a mutation running, so the worker
adds at most one background rewrite at a time. It removes only rows an
erasure already deleted: what a read sees does not change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from server.erasure.store import reclaim as reclaim_store
from server.erasure.store.mutations import LIGHTWEIGHT_PURGE_TABLES
from server.settings import settings
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TableDead:
    table: str
    on_disk: int
    visible: int
    bytes: int

    @property
    def dead_fraction(self) -> float:
        return (self.on_disk - self.visible) / self.on_disk if self.on_disk else 0.0

    @property
    def dead_bytes(self) -> int:
        return int(self.bytes * self.dead_fraction)


@dataclass(frozen=True)
class ReclaimResult:
    reclaimed: str  # the table given APPLY DELETED MASK, "" when none
    candidates: int  # tables past both thresholds


def pick(
    tables: list[TableDead], *, min_fraction: float, min_bytes: int
) -> tuple[TableDead | None, int]:
    """(the table with the most dead bytes among those past both thresholds,
    how many are past them). Pure."""
    over = [t for t in tables if t.dead_fraction >= min_fraction and t.dead_bytes >= min_bytes]
    return max(over, key=lambda t: t.dead_bytes, default=None), len(over)


async def _dead(ch: Any, table: str) -> TableDead | None:
    rows = await reclaim_store.masked_rows(ch, table)
    if rows is None:
        return None
    on_disk, visible, size = rows
    return TableDead(table=table, on_disk=on_disk, visible=visible, bytes=size)


async def run_pass() -> ReclaimResult:
    from server.core.ch import get_client

    ch = get_client()
    with record_pass("erasure_reclaim") as rec:
        tables = [d for t in sorted(LIGHTWEIGHT_PURGE_TABLES) if (d := await _dead(ch, t))]
        best, over = pick(
            tables,
            min_fraction=settings.erasure_reclaim_min_dead_fraction,
            min_bytes=settings.erasure_reclaim_min_dead_bytes,
        )
        if best is not None:
            await reclaim_store.apply_deleted_mask(ch, best.table)
            log.info(
                "erasure_reclaim: %s holds %.0f%% purged rows (~%d MiB); applying its deleted mask",
                best.table,
                100 * best.dead_fraction,
                best.dead_bytes >> 20,
            )
        rec.outcome = {"reclaimed": best.table if best else "", "candidates": over}
    return ReclaimResult(reclaimed=best.table if best else "", candidates=over)
