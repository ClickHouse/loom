"""A Parquet file as one table, read with pyarrow from the bytes in memory (no
file system, no threads): its columns from the schema, its rows a row group at
a time, in batches of at most _BATCH_ROWS rows and MAX_CELLS cells. The column
names are no row of the file, so its rows count from 1 (`tabular.Rows.header_row`
0).

A value reads as its column's type says, decided once from the schema:
- an integer is a number; a decimal is a number written as its exact digits
  (a JSON number would read back as a double and lose them), as is an integer
  past signed 64 bits (a uint64);
- a float is a number (a float32 or float16 column's its shortest decimal;
  one inside a list or struct is the double it widens to); NaN or an infinity
  is its text;
- a bool, a date, a timestamp (its zone kept, none when the column has none),
  a time of day and a duration (`H:MM:SS`, hours past a day) as the other
  readers write them; a nanosecond unit is read to the microsecond;
- text is text, a UUID its text; a list, struct or map is its JSON (bytes in
  it as base64);
- bytes are no text: left out and counted as a failed unit (`<name> values`),
  as is a value over MAX_VALUE_CHARS, and a value Python cannot hold (a date
  past year 9999), its batch's values of that column with it.

What is read is bounded: the columns past MAX_COLUMNS are left out (`<name>
columns`), the kept ones taken by position (a name may repeat); pages decode
one at a time into batches no wider than the cell budget, so a row group's
size does not decide what is held; the rows past the budget
(`tabular.Budget`) are counted from the metadata, never decoded. The
extractor's child limits (memory, CPU, no network) bound one decode (a single
huge page, a decoder fault): what does not fit fails the rows from there
(`<name> rows N-`).

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

from loom_extractor import tabular
from loom_extractor.artifact import MAX_COLUMNS, Artifact, Blocks, Coverage, Failure
from loom_extractor.tabular import EMPTY, Budget, Cell, Rows, counted, fail, table

PARSER_VERSION = "1"
MAX_VALUE_CHARS = 1 << 20  # not a setting: a value a table holds, as a CSV field
_BATCH_ROWS = 1024  # not a setting: what one decode holds, at most
_INT64 = range(-(2**63), 2**63)


class ParquetError(Exception):
    """The bytes are not a Parquet file pyarrow can read."""


def _duration(value: dt.timedelta) -> str:
    """`H:MM:SS`, hours past a day, from the exact parts (a float of seconds
    loses microseconds past about 100,000 days)."""
    micros = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    sign = "-" if micros < 0 else ""
    whole, fraction = divmod(abs(micros), 1_000_000)
    text = f"{sign}{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"
    return text + f".{fraction:06d}".rstrip("0") if fraction else text


def _plain(value: Decimal) -> str:
    """A decimal's digits, never in exponent notation (`0.0000001`, not `1E-7`)."""
    return format(value, "f")


def _shortest(value: float, code: str) -> float:
    """A float32 (`code` "f") or float16 ("e") value as its shortest decimal:
    `0.1`, not the double it widens to (`0.10000000149011612`)."""
    for digits in range(1, 9):
        short = float(f"{value:.{digits}g}")
        try:
            if struct.unpack(code, struct.pack(code, short))[0] == value:
                return short
        except OverflowError:  # rounded past the type's range (its maximum, rounded up)
            continue
    return float(f"{value:.9g}")  # nine significant digits round-trip every float32


def _json(value: Any) -> Any:
    """A nested value as JSON can hold it."""
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):  # a map reads as a list of (key, value)
        return [_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, Decimal):
        return _plain(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, (dt.date, dt.time, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return _duration(value)
    return value


# A value `_values` left out (and counted): no cell, though the row has a value there.
LEFT_OUT = object()


class _Reader:
    """Cells of one column type; counts the values it leaves out."""

    def __init__(self) -> None:
        self.left_out = 0  # bytes, a value over MAX_VALUE_CHARS, or one Python cannot hold

    def cell(self, value: Any, kind: str) -> Cell:
        if value is None or value is LEFT_OUT:
            return EMPTY
        if kind == "bytes":
            self.left_out += 1
            return EMPTY
        if kind == "nested":
            value, kind = json.dumps(_json(value), ensure_ascii=False), "string"
        if kind in ("float32", "float16"):
            if not math.isfinite(value):
                return Cell(str(value), "string")
            return Cell(_shortest(value, "f" if kind == "float32" else "e"), "number")
        if kind == "number":
            if isinstance(value, Decimal):  # an Arrow decimal is finite
                return Cell(_plain(value), "number")
            if isinstance(value, int):
                return Cell(value, "number") if value in _INT64 else Cell(str(value), "number")
            if not math.isfinite(value):
                return Cell(str(value), "string")
            return Cell(value, "number")
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
    """What a column of `arrow_type` holds: number, float32, float16, bool, date,
    datetime, time, duration, string, bytes or nested."""
    import pyarrow as pa

    t = arrow_type
    while pa.types.is_dictionary(t):
        t = t.value_type
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_float32(t):
        return "float32"
    if pa.types.is_float16(t):
        return "float16"
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
    if (
        pa.types.is_binary(t)
        or pa.types.is_large_binary(t)
        or pa.types.is_fixed_size_binary(t)
        or pa.types.is_binary_view(t)
    ):
        return "bytes"
    if pa.types.is_nested(t):
        return "nested"
    return "string"


def _readable(arrow_type: Any) -> Any:
    """The type a column is read as, so Python holds every value of it the same
    way, or None when it is read as it is: a nanosecond unit as microseconds
    (what Python's types hold; the nanoseconds go), a float16 as a float32
    (Python has no half float), inside a list, struct or map too."""
    import pyarrow as pa

    t = arrow_type
    if pa.types.is_dictionary(t):
        return _readable(t.value_type)
    if pa.types.is_timestamp(t) and t.unit == "ns":
        return pa.timestamp("us", tz=t.tz)
    if pa.types.is_time64(t) and t.unit == "ns":
        return pa.time64("us")
    if pa.types.is_duration(t) and t.unit == "ns":
        return pa.duration("us")
    if pa.types.is_float16(t):
        return pa.float32()
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t):
        inner = _readable(t.value_type)
        if inner is None:
            return None
        if pa.types.is_fixed_size_list(t):
            return pa.list_(inner, t.list_size)
        return (pa.large_list if pa.types.is_large_list(t) else pa.list_)(inner)
    if pa.types.is_map(t):
        key, item = _readable(t.key_type), _readable(t.item_type)
        if key is None and item is None:
            return None
        return pa.map_(key or t.key_type, item or t.item_type)
    if pa.types.is_struct(t):
        fields = [t.field(i) for i in range(t.num_fields)]
        inner = [_readable(f.type) for f in fields]
        if all(i is None for i in inner):
            return None
        return pa.struct([f.with_type(i or f.type) for f, i in zip(fields, inner, strict=True)])
    return None


def _values(column: Any, target: Any, reader: _Reader) -> list[Any]:
    """A batch column's values as Python holds them. A column with a value
    Python cannot (a date past year 9999, a zone it does not know) has its
    batch's values left out and counted: LEFT_OUT, not None, so a row of them
    is still a row."""
    import pyarrow as pa

    try:
        if target is not None:
            column = column.cast(target, safe=False)
        values: list[Any] = column.to_pylist()
        return values
    except (pa.ArrowException, ValueError, OverflowError, KeyError):
        reader.left_out += len(column) - column.null_count
        return [None if null else LEFT_OUT for null in column.is_null().to_pylist()]


def _open(data: bytes) -> Any:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    try:
        return pq.ParquetFile(pa.BufferReader(data))
    except (pa.ArrowException, OSError, ValueError) as e:
        raise ParquetError(str(e)[:250]) from e  # source.py names it a corrupt parquet file


def extract_parquet(data: bytes, filename: str = "") -> Artifact:
    import pyarrow as pa

    name = filename or "rows"
    pf = _open(data)
    meta = pf.metadata
    fields = list(pf.schema_arrow)
    kept = fields[:MAX_COLUMNS]
    kinds = [_kind(f.type) for f in kept]
    targets = [_readable(f.type) for f in kept]
    budget = Budget()
    failed: list[Failure] = []
    if len(fields) > MAX_COLUMNS:
        reason = f"{counted(len(fields) - MAX_COLUMNS, 'column')} past column {MAX_COLUMNS}"
        fail(budget, failed, f"{name} columns", reason)
    rows = Rows([Cell(f.name, "string") if f.name.strip() else EMPTY for f in kept], header_row=0)
    budget.collect([c for c in rows.header if c.text])  # held, as a sheet's is
    reader = _Reader()
    # No more values to a batch than the budget holds cells, however wide the
    # file: its leaf columns, a struct's fields each one, are what a batch decodes.
    batch_rows = max(1, min(_BATCH_ROWS, tabular.MAX_CELLS // max(1, meta.num_columns)))
    groups = [meta.row_group(g).num_rows for g in range(meta.num_row_groups)]
    number = blank = 0
    for g, group_rows in enumerate(groups):
        if rows.more:
            # Past the budget: counted from the metadata, never decoded.
            rows.count(number + 1, group_rows)
            number += group_rows
            continue
        start = number
        try:
            # Every column, the kept ones taken by position: a name may repeat,
            # so none is selected by name.
            for batch in pf.iter_batches(batch_size=batch_rows, row_groups=[g], use_threads=False):
                columns = [_values(batch.column(j), targets[j], reader) for j in range(len(kept))]
                for i in range(batch.num_rows):
                    if rows.more:  # past the budget: counted below, its values not read
                        break
                    number += 1
                    values = [columns[j][i] for j in range(len(kept))]
                    if all(v is None for v in values):
                        blank += 1  # a row of nothing is no row, as a blank CSV record
                        continue
                    # A row whose values were all left out (bytes, values Python
                    # cannot hold) is still a row.
                    typed = (reader.cell(values[j], kinds[j]) for j in range(len(kept)))
                    rows.add(
                        number, {j: c for j, c in enumerate(typed) if c.kind != "empty"}, budget
                    )
                if rows.more:  # the budget ran out in this batch: the rest is counted
                    rest = group_rows - (number - start)
                    rows.count(number + 1, rest)
                    number += rest
                    break
        except (pa.ArrowException, OSError, ValueError, MemoryError) as e:
            # The group's rows from the first not decoded (or not held: a list
            # value past the child's memory), and every group after.
            why = str(e) or type(e).__name__  # a MemoryError has no message
            fail(budget, failed, f"{name} rows {number + 1}-", f"not decoded: {why}"[:200])
            break
    if reader.left_out:
        reason = f"{counted(reader.left_out, 'value')} not text, over {MAX_VALUE_CHARS} characters"
        fail(budget, failed, f"{name} values", reason + " or past what Python holds")
    acc = Blocks()
    t, table_failed = table(acc, "t0", name, rows, budget)
    failed += table_failed
    return Artifact(
        format="parquet",
        parser="loom-parquet",
        parser_version=f"{PARSER_VERSION}+pyarrow{pa.__version__}",  # pyarrow decodes the pages
        blocks=tuple(acc.blocks),
        # The row groups' rows (the count each declares, as they are read), less the blank.
        coverage=Coverage(
            "rows", sum(groups) - blank, len(t.rows) if t is not None else 0, failed=tuple(failed)
        ),
        tables=(t,) if t is not None else (),
    )
