"""A Parquet file as one table, read with pyarrow from the bytes in memory (no
file system, no threads): its columns from the schema, its rows a row group at
a time, in batches of _BATCH_ROWS. The column names are no row of the file, so
its rows count from 1 (`tabular.Rows.header_row` 0).

A value reads as its type says:
- an integer is a number; one past signed 64 bits (a uint64) is its digits as
  text, as in CSV;
- a float is a number; NaN or an infinity is its text;
- a decimal is a number, written as its exact text (a JSON number would read
  back as a double and lose digits);
- a bool, a date, a timestamp (its zone kept, none when the column has none),
  a time of day and a duration (`H:MM:SS`, hours past a day) as the other
  readers write them;
- text is text; a list, struct or map is its JSON;
- bytes are no text: left out and counted as a failed unit (`<name> values`),
  as is a value over MAX_VALUE_CHARS.

What is read is bounded: the columns past MAX_COLUMNS are not read (`<name>
columns`); pages decode one at a time into batches, so a row group's size does
not decide what is held; the rows past the budget (`tabular.Budget`) are
counted from the metadata, never decoded. The extractor's child limits
(memory, CPU, no network) bound one decode (a single huge page, a decoder
fault): what does not fit fails the rows from there (`<name> rows N-`).

Pure: it reads only the bytes it is given.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import struct
from decimal import Decimal
from typing import Any

from loom_extractor.artifact import MAX_COLUMNS, Artifact, Blocks, Coverage, Failure
from loom_extractor.tabular import EMPTY, Budget, Cell, Rows, counted, fail, table

PARSER_VERSION = "1"
MAX_VALUE_CHARS = 1 << 20  # not a setting: a value a table holds, as a CSV field
_BATCH_ROWS = 1024  # not a setting: what one decode holds
_INT64 = range(-(2**63), 2**63)


class ParquetError(Exception):
    """The bytes are not a Parquet file pyarrow can read."""


def _duration(value: dt.timedelta) -> str:
    seconds = value.total_seconds()
    sign = "-" if seconds < 0 else ""
    whole = int(abs(seconds))
    text = f"{sign}{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"
    fraction = abs(seconds) - whole
    return text + f"{fraction:.6f}"[1:].rstrip("0").rstrip(".") if fraction else text


def _shortest32(value: float) -> float:
    """A float32 value as its shortest decimal: `0.1`, not the double it widens
    to (`0.10000000149011612`)."""
    for digits in range(1, 10):
        short = float(f"{value:.{digits}g}")
        if struct.unpack("f", struct.pack("f", short))[0] == value:
            return short
    return value


def _json(value: Any) -> Any:
    """A nested value as JSON can hold it."""
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):  # a map reads as a list of (key, value)
        return [_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, (dt.date, dt.time, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return _duration(value)
    return value


class _Reader:
    """Cells of one column type; counts the values it leaves out."""

    def __init__(self) -> None:
        self.left_out = 0  # bytes, or a value over MAX_VALUE_CHARS

    def cell(self, value: Any, kind: str) -> Cell:
        if value is None:
            return EMPTY
        if kind == "bytes":
            self.left_out += 1
            return EMPTY
        if kind == "nested":
            value, kind = json.dumps(_json(value), ensure_ascii=False), "string"
        if kind == "float32":
            if not math.isfinite(value):
                return Cell(str(value), "string")
            return Cell(_shortest32(value), "number")
        if kind == "number":
            if isinstance(value, int):
                return Cell(value, "number") if value in _INT64 else Cell(str(value), "string")
            if isinstance(value, Decimal):
                return (
                    Cell(str(value), "number") if value.is_finite() else Cell(str(value), "string")
                )
            if not math.isfinite(value):
                return Cell(str(value), "string")
            return Cell(float(value), "number")
        if kind in ("date", "datetime", "time"):
            return Cell(value.isoformat(), kind)
        if kind == "duration":
            return Cell(_duration(value), "duration")
        if kind == "bool":
            return Cell(bool(value), "bool")
        text = str(value)
        if len(text) > MAX_VALUE_CHARS:
            self.left_out += 1
            return EMPTY
        return Cell(text, "string") if text.strip() else EMPTY


def _kind(arrow_type: Any) -> str:
    """What a column of `arrow_type` holds: number, bool, date, datetime, time,
    duration, string, bytes or nested."""
    import pyarrow as pa

    t = arrow_type
    while pa.types.is_dictionary(t):
        t = t.value_type
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_float32(t) or pa.types.is_float16(t):
        return "float32"
    if pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "number"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_timestamp(t):
        return "datetime"
    if pa.types.is_time(t):
        return "time"
    if pa.types.is_duration(t):
        return "duration"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t):
        return "bytes"
    if pa.types.is_nested(t):
        return "nested"
    return "string"


def _values(column: Any, kinds: list[str], j: int) -> list[Any]:
    """A batch column's values as Python holds them. One Python cannot (a date
    past year 9999, a timestamp's nanoseconds) reads as Arrow's text of it, and
    its column as text."""
    import pyarrow as pa

    try:
        return list(column.to_pylist())
    except (pa.ArrowException, ValueError, OverflowError):
        kinds[j] = "string"
        return list(column.cast(pa.string()).to_pylist())


def _open(data: bytes) -> Any:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    try:
        return pq.ParquetFile(pa.BufferReader(data))
    except (pa.ArrowException, OSError, ValueError) as e:
        raise ParquetError(f"not a readable parquet file: {e}"[:300]) from e


def extract_parquet(data: bytes, filename: str = "") -> Artifact:
    import pyarrow as pa

    name = filename or "rows"
    pf = _open(data)
    meta = pf.metadata
    fields = list(pf.schema_arrow)
    kept = fields[:MAX_COLUMNS]
    kinds = [_kind(f.type) for f in kept]
    budget = Budget()
    failed: list[Failure] = []
    if len(fields) > MAX_COLUMNS:
        reason = f"{counted(len(fields) - MAX_COLUMNS, 'column')} past column {MAX_COLUMNS}"
        fail(budget, failed, f"{name} columns", reason)
    rows = Rows([Cell(f.name, "string") if f.name.strip() else EMPTY for f in kept], header_row=0)
    budget.collect([c for c in rows.header if c.text])  # held, as a sheet's is
    reader = _Reader()
    number = 0
    blank = 0
    for g in range(meta.num_row_groups):
        group = meta.row_group(g)
        if rows.more or budget.cut:
            # Past the budget: counted from the metadata, never decoded.
            rows.count(number + 1, group.num_rows)
            number += group.num_rows
            continue
        start = number
        try:
            for batch in pf.iter_batches(
                batch_size=_BATCH_ROWS,
                row_groups=[g],
                columns=[f.name for f in kept] if len(fields) > MAX_COLUMNS else None,
                use_threads=False,
            ):
                columns = [_values(batch.column(j), kinds, j) for j in range(len(kept))]
                for i in range(batch.num_rows):
                    number += 1
                    typed = (reader.cell(columns[j][i], kinds[j]) for j in range(len(kept)))
                    cells = {j: c for j, c in enumerate(typed) if c.kind != "empty"}
                    if cells:
                        rows.add(number, cells, budget)
                    else:
                        blank += 1  # a row of nothing is no row, as a blank CSV record
                if rows.more:  # the budget ran out in this batch: the rest is counted
                    rest = group.num_rows - (number - start)
                    rows.count(number + 1, rest)
                    number += rest
                    break
        except (pa.ArrowException, OSError, ValueError) as e:
            # The group's rows from the first not decoded, and every group after.
            fail(budget, failed, f"{name} rows {number + 1}-", f"not decoded: {e}"[:200])
            number = meta.num_rows
            break
    if reader.left_out:
        reason = (
            f"{counted(reader.left_out, 'value')} not text or over {MAX_VALUE_CHARS} characters"
        )
        fail(budget, failed, f"{name} values", reason)
    acc = Blocks()
    t, table_failed = table(acc, "t0", name, rows, budget)
    failed += table_failed
    tables = (t,) if t is not None else ()
    return Artifact(
        format="parquet",
        parser="loom-parquet",
        parser_version=f"{PARSER_VERSION}+pyarrow{pa.__version__}",  # pyarrow decodes the pages
        blocks=tuple(acc.blocks),
        coverage=Coverage(
            "rows", meta.num_rows - blank, len(t.rows) if t is not None else 0, failed=tuple(failed)
        ),
        tables=tables,
    )
