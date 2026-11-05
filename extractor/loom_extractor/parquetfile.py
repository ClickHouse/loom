"""A Parquet file as one table, read with pyarrow from the bytes in memory (no
file system, no threads): its columns from the schema, its rows a row group at
a time, in batches of at most _BATCH_ROWS rows and MAX_CELLS cells. The column
names are no row of the file, so its rows count from 1 (`tabular.Rows.header_row`
0).

A value reads as its column's type says, decided once from the schema:
- an integer is a number; a decimal is a number written as its exact digits
  (a JSON number would read back as a double and lose them), as is an integer
  past signed 64 bits (a uint64);
- a float is a number (a float32 or float16 column's value is its shortest
  decimal; one inside a list or struct is the double it widens to); a NaN is
  no value, as a null is, and an infinity is left out and counted;
- a bool, a date, a timestamp (its zone kept, none when the column has none),
  a time of day and a duration (`H:MM:SS`, hours past a day) as the other
  readers write them; a nanosecond unit is read to the microsecond;
- text is text, a UUID its text; a list, struct or map is its JSON (bytes in
  it as base64);
- bytes are no text: left out and counted as a failed unit (`<name> values`),
  as is a value over MAX_VALUE_CHARS, and a value Python cannot hold (a date
  past year 9999), that value alone (`_split`), and past _MAX_UNHELD of them
  the rest of its column, unread.

What is read is bounded: the columns past MAX_COLUMNS are left out (`<name>
columns`), the kept ones taken by position (a name may repeat); pages decode
one at a time into batches no wider than the cell budget, so a row group's
size does not decide what is held; the rows past the budget
(`tabular.Budget`), and past _MAX_DECODED values or _MAX_DECODED_BYTES decoded
or _MAX_GROUPS row groups read (a file of blank rows, or of values too long to
hold, is no cheaper to read), are counted from the metadata, never decoded. The
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
import uuid
from decimal import Decimal
from typing import Any

from loom_extractor import tabular
from loom_extractor.artifact import MAX_COLUMNS, Artifact, Blocks, Coverage, Failure
from loom_extractor.tabular import EMPTY, Budget, Cell, Rows, counted, fail, table

PARSER_VERSION = "1"
MAX_VALUE_CHARS = 1 << 20  # not a setting: a value a table holds, as a CSV field
_BATCH_ROWS = 1024  # not a setting: what one decode holds, at most
# The values and the bytes a file decodes, blank rows' and values left out
# among them: past either, the rest is counted from the metadata, not read.
_MAX_DECODED = 4 * tabular.MAX_CELLS  # not a setting: a bound on hostile input
_MAX_DECODED_BYTES = tabular.MAX_HELD_BYTES  # not a setting: a bound on hostile input
# The values Python cannot hold that a column is searched for, across the file;
# past them the rest of the column is left out unread.
_MAX_UNHELD = 16  # not a setting: a bound on hostile input
# The row groups a file has read: each costs its reader's setup, whatever it holds.
_MAX_GROUPS = tabular.MAX_ROWS  # not a setting: a bound on hostile input
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
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


# A value `_values` left out (and counted): no cell, though the row has a value there.
_LEFT_OUT = object()
# One it did not convert at all: past _MAX_UNHELD values Python cannot hold in
# its column, across the file.
_UNREAD = object()


class _Reader:
    """Cells of the rows read; counts the values it leaves out."""

    def __init__(self) -> None:
        # The values left out of the rows read, by why.
        self.left_out = {"bytes": 0, "infinite": 0, "long": 0, "unheld": 0, "unread": 0}
        self.total = 0  # their sum, so a row can tell whether it left one out

    def leave(self, cause: str) -> Cell:
        self.left_out[cause] += 1
        self.total += 1
        return EMPTY

    def cell(self, value: Any, kind: str) -> Cell:
        if value is None:
            return EMPTY
        if value is _LEFT_OUT:
            return self.leave("bytes" if kind == "bytes" else "unheld")
        if value is _UNREAD:
            return self.leave("unread")
        if kind == "nested":
            # str: a value of a type JSON does not know (one an extension adds) as its text.
            value, kind = json.dumps(_json(value), ensure_ascii=False, default=str), "string"
        if kind in ("float32", "float16", "number") and isinstance(value, float):
            # A NaN is a float column's mark of no value (a dataframe writer's for a
            # missing number), so it is a blank cell; an infinity is no number a
            # cell holds, so it is left out. Either way the column stays a number.
            if math.isnan(value):
                return EMPTY
            if math.isinf(value):
                return self.leave("infinite")
            if kind != "number":
                return Cell(_shortest(value, "f" if kind == "float32" else "e"), "number")
        if kind == "number":
            if isinstance(value, Decimal):  # an Arrow decimal is finite
                return Cell(_plain(value), "number")
            if isinstance(value, int):
                return Cell(value, "number") if value in _INT64 else Cell(str(value), "number")
            return Cell(value, "number")
        if kind in ("date", "datetime", "time"):
            return Cell(value.isoformat(), kind)
        if kind == "duration":
            return Cell(_duration(value), "duration")
        if kind == "bool":
            return Cell(bool(value), "bool")
        text = str(value)
        if len(text) > MAX_VALUE_CHARS:
            return self.leave("long")
        return Cell(text, "string") if text.strip() else EMPTY


def _stored(arrow_type: Any) -> Any:
    """The type a column's values are read by: an extension type's storage
    (its values are those of what it extends), but a UUID, read as its text,
    and a bool8, a bool."""
    import pyarrow as pa

    t = arrow_type
    while pa.types.is_dictionary(t):
        t = t.value_type
    if isinstance(t, pa.BaseExtensionType):
        if t.extension_name == "arrow.uuid":
            return t
        if t.extension_name == "arrow.bool8":
            return pa.bool_()
        return _stored(t.storage_type)
    return t


def _kind(arrow_type: Any) -> str:
    """What a column of `arrow_type` holds: number, float32, float16, bool, date,
    datetime, time, duration, string (a UUID among them), bytes or nested."""
    import pyarrow as pa

    t = _stored(arrow_type)
    if isinstance(t, pa.BaseExtensionType):  # a UUID
        return "string"
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
    if isinstance(t, pa.BaseExtensionType):
        stored = _stored(t)
        if stored is t:  # a UUID: read as it is
            return None
        inner = _readable(stored)
        return stored if inner is None else inner
    if pa.types.is_timestamp(t) and t.unit == "ns":
        return pa.timestamp("us", tz=t.tz)
    if pa.types.is_time64(t) and t.unit == "ns":
        return pa.time64("us")
    if pa.types.is_duration(t) and t.unit == "ns":
        return pa.duration("us")
    if pa.types.is_float16(t):
        return pa.float32()
    # A list view stays as it is: Arrow casts none to another element type.
    for is_list, make in ((pa.types.is_list, pa.list_), (pa.types.is_large_list, pa.large_list)):
        if is_list(t):
            inner = _readable(t.value_type)
            return None if inner is None else make(inner)
    if pa.types.is_fixed_size_list(t):
        inner = _readable(t.value_type)
        return None if inner is None else pa.list_(inner, t.list_size)
    if pa.types.is_map(t):
        key, item = _readable(t.key_type), _readable(t.item_type)
        if key is None and item is None:
            return None
        return pa.map_(
            key if key is not None else t.key_type, item if item is not None else t.item_type
        )
    if pa.types.is_struct(t):
        fields = [t.field(i) for i in range(t.num_fields)]
        inner = [_readable(f.type) for f in fields]
        if all(i is None for i in inner):
            return None
        return pa.struct(
            [f.with_type(f.type if i is None else i) for f, i in zip(fields, inner, strict=True)]
        )
    return None


def _values(column: Any, target: Any, kind: str, unheld: list[int], j: int) -> list[Any]:
    """A batch column's values as Python holds them. Bytes, and a value Python
    cannot hold (a date past year 9999, a zone it does not know), are _LEFT_OUT,
    not None, so a row of them is still a row; the rows read count them
    (`_Reader.cell`). Bytes are never copied into Python. A batch with a value
    Python cannot hold is split in halves until each part converts or is one
    value, so only that value is left out; past _MAX_UNHELD such values in the
    column (`unheld[j]`, kept across its batches), the rest of it is left out
    unread."""
    import pyarrow as pa

    if kind == "bytes":
        return _left_out(column)
    if isinstance(column.type, pa.BaseExtensionType) and _stored(column.type) is not column.type:
        column = column.storage
    return _split(column, target, unheld, j)


def _left_out(column: Any) -> list[Any]:
    return [None if null else _LEFT_OUT for null in column.is_null().to_pylist()]


def _split(column: Any, target: Any, unheld: list[int], j: int) -> list[Any]:
    """`column` converted, or, where it fails, each half of it: a value that
    fails alone is _LEFT_OUT (and counted in `unheld[j]`, its column's), and
    past _MAX_UNHELD of them what is left is _UNREAD."""
    import pyarrow as pa

    if unheld[j] >= _MAX_UNHELD:
        return [None if null else _UNREAD for null in column.is_null().to_pylist()]
    try:
        if target is not None:
            column = column.cast(target, safe=False)
        values: list[Any] = column.to_pylist()
        return values
    except MemoryError:  # Arrow's too: a decode past the child's memory fails its rows
        raise
    except (pa.ArrowException, ValueError, OverflowError, KeyError):
        if len(column) == 1:
            unheld[j] += 1
            return [_LEFT_OUT]
    half = len(column) // 2
    return _split(column.slice(0, half), target, unheld, j) + _split(
        column.slice(half), target, unheld, j
    )


def _cost(array: Any) -> tuple[int, int]:
    """What reading `array` into Python costs, before any of it is: its values,
    every element of a list, struct or map counted down to its leaves, and the
    bytes a string or binary dictionary copies, an entry once for each value
    that names it (a list of nulls has no buffer, a dictionary entry one)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    t = array.type
    if isinstance(t, pa.BaseExtensionType):
        return _cost(array.storage)
    if pa.types.is_dictionary(t):
        copies = 0
        copied = (
            pa.types.is_string,
            pa.types.is_large_string,
            pa.types.is_string_view,
            pa.types.is_binary,
            pa.types.is_large_binary,
            pa.types.is_binary_view,
        )
        if any(is_copied(t.value_type) for is_copied in copied):
            sizes = pc.take(_lengths(array.dictionary), array.indices)
            copies = int(pc.sum(sizes).as_py() or 0)
        return len(array), copies
    if pa.types.is_map(t):
        keys, items = _cost(array.keys), _cost(array.items)
        return len(array) + keys[0] + items[0], keys[1] + items[1]
    lists = (
        pa.types.is_list,
        pa.types.is_large_list,
        pa.types.is_fixed_size_list,
        pa.types.is_list_view,
        pa.types.is_large_list_view,
    )
    if any(is_list(t) for is_list in lists):
        values, copies = _cost(array.flatten())
        return len(array) + values, copies
    if pa.types.is_struct(t):
        parts = [_cost(f) for f in array.flatten()]
        return len(array) + sum(v for v, _ in parts), sum(c for _, c in parts)
    if pa.types.is_string_view(t) or pa.types.is_binary_view(t):
        return len(array), _shared(array)
    return len(array), 0


def _lengths(values: Any) -> Any:
    """Each string or binary value's length in bytes, read without copying one;
    a view's from its view, whose first four bytes are its length (Arrow's view
    layout: Arrow has no length kernel for views, and casting would copy the
    bytes views share)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    if not (pa.types.is_string_view(values.type) or pa.types.is_binary_view(values.type)):
        return pc.binary_length(values)
    n, views = values.offset + len(values), values.buffers()[1]
    if len(values) == 0 or views is None:
        return pa.array([0] * len(values), pa.int32())
    words = pa.Array.from_buffers(pa.int32(), 4 * n, [None, views])  # a view is four int32s
    lengths = pc.list_element(pa.FixedSizeListArray.from_arrays(words, 4), 0)
    return pc.if_else(values.is_null(), 0, lengths.slice(values.offset))


def _shared(views: Any) -> int:
    """The bytes a view array's values are beyond its data buffers, which views
    may share: what `nbytes` does not count and Python copies, a value each."""
    import pyarrow.compute as pc

    data = sum(b.size for b in views.buffers()[2:] if b is not None)
    return max(0, int(pc.sum(_lengths(views)).as_py() or 0) - int(data))


def _open(data: bytes) -> Any:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    try:
        return pq.ParquetFile(pa.BufferReader(data))
    except (pa.ArrowException, OSError, ValueError) as e:
        raise ParquetError(str(e)[:250]) from e  # source.py names it a corrupt parquet file


class _Walk:
    """One file's rows as they are read: the numbering, the blank rows, what has
    been decoded so far and what failed, across its row groups."""

    def __init__(self, pf: Any, name: str, kept: list[Any], rows: Rows, budget: Budget) -> None:
        self.pf, self.name, self.rows, self.budget = pf, name, rows, budget
        self.kinds = [_kind(f.type) for f in kept]
        self.targets = [_readable(f.type) for f in kept]
        self.unheld = [0] * len(kept)  # each column's values Python cannot hold, across the file
        self.reader = _Reader()
        self.failed: list[Failure] = []
        self.number = self.blank = self.decoded = self.decoded_bytes = 0
        # No more values to a batch than the budget holds cells, however wide the
        # file: its leaf columns, a struct's fields each one, are what a batch decodes.
        self.batch_rows = max(
            1, min(_BATCH_ROWS, tabular.MAX_CELLS // max(1, pf.metadata.num_columns))
        )

    def fail(self, unit: str, reason: str) -> None:
        fail(self.budget, self.failed, f"{self.name} {unit}", reason)

    def _fail_from(self, reason: str) -> None:
        """The rows from the next one on are a failed unit."""
        self.fail(f"rows {self.number + 1}-", reason)

    def read(self, groups: list[int]) -> None:
        """Read the row groups (`groups`, each one's row count) to the first bound."""
        if not self.kinds:  # no column: every row is blank, counted from the metadata
            self.blank = sum(groups)
            return
        read_groups = 0
        for g, group_rows in enumerate(groups):
            if not group_rows:  # nothing to decode: no reader is set up for it
                continue
            if self.rows.more:
                # Past the budget: counted from the metadata, never decoded.
                self.rows.count(self.number + 1, group_rows)
                self.number += group_rows
                continue
            read_groups += 1
            # Each costs a reader's setup: past them, the rest is counted.
            if read_groups > _MAX_GROUPS:
                self._fail_from(f"{counted(sum(groups[g:]), 'row')} past what one file decodes")
                return
            if self._group(groups, g):
                return

    def _group(self, groups: list[int], g: int) -> bool:
        """Read row group `g`; True when the file is read no further."""
        import pyarrow as pa

        start, group_rows = self.number, groups[g]
        try:
            # Every column, the kept ones taken by position: a name may repeat,
            # so none is selected by name.
            batches = self.pf.iter_batches(
                batch_size=self.batch_rows, row_groups=[g], use_threads=False
            )
            for batch in batches:
                # A file of blank rows, of long lists or of values each too long to
                # hold is read no further: the rest is its own failed unit.
                if self._charge(batch):  # this group's rest and every group after
                    rest = group_rows - (self.number - start) + sum(groups[g + 1 :])
                    self._fail_from(f"{counted(rest, 'row')} past what one file decodes")
                    return True
                self._read(batch)
                if self.rows.more:  # the budget ran out in this batch: the rest is counted
                    rest = group_rows - (self.number - start)
                    self.rows.count(self.number + 1, rest)
                    self.number += rest
                    return False
        except (pa.ArrowException, OSError, ValueError, MemoryError) as e:
            # The group's rows from the first not decoded (or not held: a list
            # value past the child's memory), and every group after.
            why = str(e) or type(e).__name__  # a MemoryError has no message
            self._fail_from(f"not decoded: {why}"[:200])
            return True
        return False

    def fail_left_out(self) -> None:
        """The values left out of the rows read, a failed unit naming each cause."""
        causes = {
            "bytes": "of bytes, no text",
            "infinite": "infinite, no number a cell holds",
            "long": f"over {MAX_VALUE_CHARS} characters",
            "unheld": "past what Python holds",
            "unread": f"left out unread past {_MAX_UNHELD} values Python cannot hold in a column",
        }
        counts = self.reader.left_out.items()
        reasons = [f"{counted(n, 'value')} {causes[k]}" for k, n in counts if n]
        if reasons:
            self.fail("values", "; ".join(reasons))

    def _charge(self, batch: Any) -> bool:
        """Charge `batch` to the decode bound before it is read into Python;
        True when it is past it. Every column's values, those past MAX_COLUMNS
        too (they are decoded), and at least a value a row, so a batch of empty
        rows costs too; the copies only of the columns read into Python (a bytes
        column, or one past MAX_COLUMNS, is never copied)."""
        costs = [_cost(batch.column(j)) for j in range(batch.num_columns)]
        copies = (
            c for j, (_, c) in enumerate(costs[: len(self.kinds)]) if self.kinds[j] != "bytes"
        )
        self.decoded += max(batch.num_rows, sum(v for v, _ in costs))
        self.decoded_bytes += int(batch.nbytes) + sum(copies)
        return self.decoded > _MAX_DECODED or self.decoded_bytes > _MAX_DECODED_BYTES

    def _read(self, batch: Any) -> None:
        kinds, reader = self.kinds, self.reader
        columns = [
            _values(batch.column(j), self.targets[j], kinds[j], self.unheld, j)
            for j in range(len(kinds))
        ]
        for i in range(batch.num_rows):
            if self.rows.more:  # past the budget: counted by the caller, its rows not read
                return
            self.number += 1
            left = reader.total
            typed = (reader.cell(columns[j][i], kinds[j]) for j in range(len(kinds)))
            cells = {j: c for j, c in enumerate(typed) if c.kind != "empty"}
            # A row of nothing (nulls, NaNs, blank text) is no row, as a blank
            # CSV record; one whose values were left out is still a row.
            if not cells and reader.total == left:
                self.blank += 1
                continue
            self.rows.add(self.number, cells, self.budget)


def extract_parquet(data: bytes, filename: str = "") -> Artifact:
    import pyarrow as pa

    name = filename or "rows"
    pf = _open(data)
    meta = pf.metadata
    fields = list(pf.schema_arrow)
    kept = fields[:MAX_COLUMNS]
    budget = Budget()
    rows = Rows([Cell(f.name, "string") if f.name.strip() else EMPTY for f in kept], header_row=0)
    walk = _Walk(pf, name, kept, rows, budget)
    if len(fields) > MAX_COLUMNS:
        walk.fail(
            "columns", f"{counted(len(fields) - MAX_COLUMNS, 'column')} past column {MAX_COLUMNS}"
        )
    budget.collect([c for c in rows.header if c.text])  # held, as a sheet's is
    # A count a footer declares below 0 is no rows.
    groups = [max(0, meta.row_group(g).num_rows) for g in range(meta.num_row_groups)]
    walk.read(groups)
    walk.fail_left_out()
    acc = Blocks()
    t, table_failed = table(acc, "t0", name, rows, budget)
    return Artifact(
        format="parquet",
        parser="loom-parquet",
        parser_version=f"{PARSER_VERSION}+pyarrow{pa.__version__}",  # pyarrow decodes the pages
        blocks=tuple(acc.blocks),
        # The row groups' rows (the count each declares, as they are read), less the blank.
        coverage=Coverage(
            "rows",
            sum(groups) - walk.blank,
            len(t.rows) if t is not None else 0,
            failed=(*walk.failed, *table_failed),
        ),
        tables=(t,) if t is not None else (),
    )
