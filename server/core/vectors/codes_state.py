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

The same read records the width each coded column was created at, which the
write guards check (`refuse_width`, `refuse_other_widths`): ClickHouse refuses
a whole insert that carries a vector of another width.

An unknown state (never probed, or the last probe failed) answers "apply":
the flag is right on every coded, patch-free table, and the cost of sending it
to a patched one (a few percent) is far below the cost of withholding it from a
patch-free one (several times the bytes read). The main fact
leg keeps its own stricter gate (`patch_parts.fact_codes_first`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection
from typing import Any

from server.core.schema.vector_codec_ddl import CODED_TABLES, codec_width
from server.core.vectors.code_tables import vector_dim_for_table

log = logging.getLogger(__name__)

CODED_COLUMNS_SQL = (
    "SELECT table, name, compression_codec FROM system.columns "
    "WHERE database = {db:String} AND compression_codec LIKE '%Quantized(%'"
)
PATCH_PARTS_SQL = (
    "SELECT table, countIf(startsWith(name, 'patch')) AS patch_parts "
    "FROM system.parts "
    "WHERE database = {db:String} AND active AND table IN {tables:Array(String)} "
    "GROUP BY table"
)

# "loom.table.column" of every coded vector column; None = never read.
_coded: frozenset[str] | None = None
# "loom.table.column" -> the vector width its codec fixes; a write of any other
# length fails.
_widths: dict[str, int] = {}
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
    _widths = {}
    _probed_at = None
    _ch = None
    _refresh = None


def set_coded(columns: set[str] | frozenset[str] | None) -> None:
    """Test seam and probe sink: qualified `db.table.column` names."""
    global _coded
    _coded = None if columns is None else frozenset(columns)


def coded_columns() -> frozenset[str] | None:
    return _coded


def _table(column: str) -> str:
    return column.rsplit(".", 1)[0]


def written_by_served_model(table: str) -> bool:
    """One of Loom's own coded tables that no label routes elsewhere: the served
    model writes it, and a model change rebuilds it at the new width."""
    table = _qualify(table)
    return table.split(".", 1)[1] in CODED_TABLES and not vector_dim_for_table(table)


def stored_width(table: str, column: str) -> int | None:
    """The width `table.column` was coded at; None when plain or never probed."""
    return _widths.get(f"{_qualify(table)}.{column}")


def served_model_writes_coded() -> bool:
    """Whether a coded column takes the served model's writes, so a model change
    must pick a width the codec can encode."""
    return any(written_by_served_model(_table(c)) for c in _widths)


def width_mismatches(served: int) -> dict[str, tuple[int, int]]:
    """Serving columns whose width differs from what their writes carry, as
    column -> (stored, written): a routed label's width, or the served model's.
    Other tables (rebuild staging, backups) take no writes."""
    out: dict[str, tuple[int, int]] = {}
    for column, stored in _widths.items():
        written = vector_dim_for_table(_table(column)) or (
            served if written_by_served_model(_table(column)) else 0
        )
        if written and stored != written:
            out[column] = (stored, written)
    return out


def _refuse(column: str, stored: int, written: int) -> None:
    from server.core.errors import CodedWidthMismatchError

    raise CodedWidthMismatchError(
        f"{column} stores {stored}-dim vectors and this write carries {written}; "
        "/v1/health says how to fix it",
        column=column,
        stored=stored,
        written=written,
    )


def _coded_of(table: str) -> list[tuple[str, str, int]]:
    """(qualified column, column name, stored width) of `table`'s coded columns,
    as the write guards read them; a stale reading is refreshed in the background,
    as for reads, so a process that only writes sees a rebuild too."""
    _maybe_refresh()
    prefix = f"{_qualify(table)}."
    return [(c, c[len(prefix) :], w) for c, w in _widths.items() if c.startswith(prefix)]


# A write that embeds is judged against a reading at most this old before any
# part of it lands, so a table rebuilt outside Loom does not leave half a write
# behind for a whole probe interval.
FRESH_READING_S = 5.0


async def _recheck() -> None:
    """Re-read the widths when the reading is older than FRESH_READING_S;
    concurrent writers wait on one re-read."""
    if _ch is None or (_probed_at is not None and time.monotonic() - _probed_at <= FRESH_READING_S):
        return
    await asyncio.shield(_start_refresh())


async def refuse_other_widths(table: str, rows: list[dict[str, Any]]) -> None:
    """Raise CodedWidthMismatchError when a row carries a vector its coded column
    cannot take; ClickHouse would fail the whole insert with code 190."""
    await _refuse_if(lambda: _row_mismatch(table, rows))


async def only_placeholders_missed(
    table: str, rows: list[dict[str, Any]], padded: list[tuple[int, str]], error: Exception
) -> bool:
    """After ClickHouse refused `rows` with code 190 (a table rebuilt at another
    width since the last reading): read the widths afresh and refuse by name when
    a vector the caller wrote misses them. True when only the placeholders at
    `padded` miss them, so the caller sizes them again and retries."""
    from server.core.ch_errors import WIDTH_MISMATCH_CODE, error_code

    if _ch is None or error_code(error) != WIDTH_MISMATCH_CODE:
        return False
    await asyncio.shield(_start_refresh(fresh=True))
    if (found := _row_mismatch(table, rows, skip=set(padded))) is not None:
        _refuse(*found)
    return any(
        len(rows[i][name]) != stored
        for _, name, stored in _coded_of(table)
        for i, c in padded
        if c == name
    )


async def refuse_width(table: str, dim: int) -> None:
    """The same check for a write whose vectors will be `dim` wide, made before
    any part of it is written, against a recent reading."""
    await _recheck()
    if (found := _width_mismatch(table, dim)) is not None:
        _refuse(*found)


def _row_mismatch(
    table: str, rows: list[dict[str, Any]], skip: Collection[tuple[int, str]] = ()
) -> tuple[str, int, int] | None:
    for column, name, stored in _coded_of(table):
        for i, row in enumerate(rows):
            vector = row.get(name)
            if (
                vector is not None
                and len(vector)
                and len(vector) != stored
                and (i, name) not in skip
            ):
                return column, stored, len(vector)
    return None


def _width_mismatch(table: str, dim: int) -> tuple[str, int, int] | None:
    for column, _, stored in _coded_of(table):
        if stored != dim:
            return column, stored, dim
    return None


async def _refuse_if(mismatch: Callable[[], tuple[str, int, int] | None]) -> None:
    if mismatch() is None:
        return
    await _recheck()
    if (found := mismatch()) is not None:
        _refuse(*found)


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
        _widths = {
            f"loom.{r['table']}.{r['name']}": width
            for r in rows
            if (width := codec_width(str(r.get("compression_codec") or ""))) is not None
        }
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


async def ensure_probed(ch: Any, *, reread: bool = False) -> None:
    """Probe what this process has not probed yet: the replica check (does
    every replica know the setting name) and this module's two facts, those
    again with `reread`.
    `CHClient.connect()` calls it, so every process that talks to ClickHouse —
    server, workers, a CLI, a bench importing the services — builds the same
    vector reads; a process that skipped the probe would send every chunk read
    without the codes flag. Never raises."""
    from server.core.vectors import quantized_setting

    try:
        if quantized_setting.quantized_codes_state() is None:
            await quantized_setting.probe_vector_search_settings(ch)
        if reread or _probed_at is None:
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
    if _ch is None or _probed_at is None or time.monotonic() - _probed_at < _ttl():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _start_refresh()


def _start_refresh(*, fresh: bool = False) -> asyncio.Task[Any]:
    """The re-probe in flight, started when there is none; with `fresh`, a new
    one even then, since one that started earlier may predate what it must see."""
    global _refresh
    if fresh or _refresh is None or _refresh.done():
        from server.core.background import detached_task, register_deferred_task

        # Process work a statement happened to trigger: its system-table reads
        # carry their own tag, not the request's.
        _refresh = register_deferred_task(detached_task(_reprobe(_ch), name="vector-codes-reprobe"))
    return _refresh


async def _reprobe(ch: Any) -> None:
    from server.core.ch import set_query_tag

    set_query_tag("vector_codes_state.reprobe")
    await probe(ch)


__all__ = [
    "CODED_COLUMNS_SQL",
    "PATCH_PARTS_SQL",
    "codes_apply",
    "coded_columns",
    "served_model_writes_coded",
    "stored_width",
    "width_mismatches",
    "written_by_served_model",
    "ensure_probed",
    "is_coded",
    "probe",
    "only_placeholders_missed",
    "refuse_other_widths",
    "refuse_width",
    "reset",
    "set_coded",
]
