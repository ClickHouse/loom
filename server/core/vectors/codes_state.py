"""Which vector reads the codes flag actually helps.

`vector_search_use_quantized_codes = 1` turns an `ORDER BY <distance> LIMIT k`
scan into a codes-first shortlist of `k x vector_search_index_fetch_multiplier`
rows plus a full-precision rescore — but only when two things hold for the
table the statement reads:

1. the vector column carries a `Quantized(...)` codec. On a plain column the
   flag is a measured no-op (code_summaries: 614.6 MiB read either way),
   so sending it only makes the SQL claim a read it does not do;
2. the table has no active patch part. ClickHouse skips the lazy rescore
   under a patch part (ClickHouse issue #119885) and the flag then reads
   MORE than a plain scan: loom.chunks with one patch part 380 vs 358 MiB,
   memory_embeddings 304 vs 177 ms.

This module holds both facts per table, read from `system.columns` and
`system.parts` in two statements, and refreshes them in the background every
`vector_codes_state_probe_seconds`. `codes_apply(table, column)` is what
`search_settings.vector_search_settings(table=...)` asks.

An unknown state (never probed, or the last probe failed) answers "apply":
the flag is right on every coded, patch-free table, and the cost of sending it
to a patched one (a few percent) is far below the cost of withholding it from a
patch-free one (several times the bytes read). The main fact
leg keeps its own stricter gate (`patch_parts.fact_codes_first`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

CODED_COLUMNS_SQL = (
    "SELECT table, name, compression_codec FROM system.columns "
    "WHERE database = {db:String} AND compression_codec LIKE '%Quantized(%'"
)
# The width argument of Quantized('<method>', <dim>, ...).
_WIDTH = re.compile(r"Quantized\([^,]*,\s*(\d+)")
PATCH_PARTS_SQL = (
    "SELECT table, countIf(startsWith(name, 'patch')) AS patch_parts "
    "FROM system.parts "
    "WHERE database = {db:String} AND active AND table IN {tables:Array(String)} "
    "GROUP BY table"
)

# "loom.table.column" of every coded vector column; None = never read.
_coded: frozenset[str] | None = None
# The vector widths those codecs fix; a write of any other length fails.
_widths: frozenset[int] = frozenset()
# monotonic time of the last completed probe; None = never probed
_probed_at: float | None = None
_ch: Any = None
_refresh: asyncio.Task[Any] | None = None


def _qualify(table: str) -> str:
    return table if "." in table else f"loom.{table}"


def reset() -> None:
    """Test seam."""
    global _coded, _widths, _probed_at, _ch, _refresh
    _coded = None
    _widths = frozenset()
    _probed_at = None
    _ch = None
    _refresh = None


def set_coded(columns: set[str] | frozenset[str] | None) -> None:
    """Test seam and probe sink: qualified `db.table.column` names."""
    global _coded
    _coded = None if columns is None else frozenset(columns)


def coded_columns() -> frozenset[str] | None:
    return _coded


def coded_widths() -> frozenset[int]:
    """The vector lengths the coded columns accept (empty: none coded, or never probed)."""
    return _widths


def is_coded(table: str, column: str) -> bool | None:
    """True / False from the last probe; None when never probed."""
    if _coded is None:
        return None
    return f"{_qualify(table)}.{column}" in _coded


def codes_apply(table: str, column: str = "embedding") -> bool:
    """Whether the codes flag pays on this read (the module docstring's rule).
    Kicks a background refresh when the reading is older than the TTL."""
    _maybe_refresh()
    from server.core.vectors import patch_parts

    full = _qualify(table)
    if is_coded(full, column) is False:
        return False
    patches = patch_parts.last_reading(full)
    return not (patches is not None and patches > 0)


async def probe(ch: Any) -> None:
    """Read both facts once. A failed read keeps the previous reading."""
    global _probed_at, _ch, _widths
    from server.core.vectors import patch_parts

    _ch = ch
    try:
        rows = await ch.query_all(CODED_COLUMNS_SQL, {"db": "loom"})
        set_coded({f"loom.{r['table']}.{r['name']}" for r in rows})
        found = (_WIDTH.search(str(r.get("compression_codec") or "")) for r in rows)
        _widths = frozenset(int(m.group(1)) for m in found if m)
    except Exception as e:  # noqa: BLE001 — advisory; the previous reading stands
        log.info("coded-column probe failed: %s", str(e)[:160])
    tables = sorted({c.rsplit(".", 1)[0].split(".", 1)[1] for c in (_coded or ())})
    if tables:
        try:
            rows = await ch.query_all(PATCH_PARTS_SQL, {"db": "loom", "tables": tables})
            seen = {str(r["table"]): int(r.get("patch_parts") or 0) for r in rows}
            for t in tables:
                # a coded table with no active part at all has no patch part
                patch_parts.set_reading(seen.get(t, 0), f"loom.{t}")
        except Exception as e:  # noqa: BLE001 — keeps the last patch-part reading
            log.info("patch-part probe failed: %s", str(e)[:160])
    _probed_at = time.monotonic()


async def ensure_probed(ch: Any) -> None:
    """Probe what this process has not probed yet: the replica check (does
    every replica know the setting name) and this module's two facts.
    `CHClient.connect()` calls it, so every process that talks to ClickHouse —
    server, workers, a CLI, a bench importing the services — builds the same
    vector reads; a process that skipped the probe would send every chunk read
    without the codes flag. Never raises."""
    from server.core.vectors import quantized_setting

    try:
        if quantized_setting.quantized_codes_state() is None:
            await quantized_setting.probe_vector_search_settings(ch)
        if _probed_at is None:
            await probe(ch)
    except Exception as e:  # noqa: BLE001 — a probe must never fail a connect
        log.info("vector read-state probe failed: %s", str(e)[:160])


def _ttl() -> float:
    from server.settings import settings

    return float(settings.vector_codes_state_probe_seconds)


def _maybe_refresh() -> None:
    """Schedule one background re-probe when the reading is stale. Only in a
    process that probed once (it holds a client) and has a running loop; a
    statement is never delayed by it."""
    global _refresh
    if _ch is None or _probed_at is None or time.monotonic() - _probed_at < _ttl():
        return
    if _refresh is not None and not _refresh.done():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    from server.core.background import detached_task, register_deferred_task

    # Process work a statement happened to trigger: its system-table reads
    # carry their own tag, not the request's.
    _refresh = register_deferred_task(detached_task(_reprobe(_ch), name="vector-codes-reprobe"))


async def _reprobe(ch: Any) -> None:
    from server.core.ch import set_query_tag

    set_query_tag("vector_codes_state.reprobe")
    await probe(ch)


__all__ = [
    "CODED_COLUMNS_SQL",
    "PATCH_PARTS_SQL",
    "codes_apply",
    "coded_columns",
    "coded_widths",
    "ensure_probed",
    "is_coded",
    "probe",
    "reset",
    "set_coded",
]
