"""A table of an extraction artifact as a typed dataset: its schema (each
column's type and the slot of `loom.dataset_rows` that holds it), what about
the schema needs a person's answer (`clarifications`), and its rows as slot
values, a row that a value does not fit rejected and counted.

A column's type is the extractor's inference, or an approval's override. A
`mixed` column is held as text until someone says what it is. A row with a
cell error, or a value its column's type cannot hold, is rejected whole, so a
dataset's sums and counts are over rows whose every value is typed.

Pure and I/O-free.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Context, Decimal, InvalidOperation
from typing import Any, Literal, cast

from server.knowledge.domain.extract.artifact import (
    MAX_COLUMN_CHARS,
    MAX_COLUMNS,
    Artifact,
    ColumnType,
    Table,
)

# The types a dataset column can be given; `mixed` is the extractor's word for
# a column whose values differ, held as text until an approval types it.
DatasetType = Literal["string", "number", "bool", "date", "datetime", "time", "duration"]

# The typed slots of loom.dataset_rows (schemas/migrations/083_knowledge_datasets.sql).
SLOTS: dict[str, int] = {"s": 24, "n": 24, "f": 8, "t": 8, "b": 8}
SLOT_COLUMNS: tuple[str, ...] = tuple(f"{k}{i}" for k, n in SLOTS.items() for i in range(1, n + 1))
# The slot kinds a type is held in, first choice first.
_KINDS: dict[str, tuple[str, ...]] = {
    "string": ("s",),
    "time": ("s",),
    "number": ("n", "f"),
    "duration": ("n", "f"),
    "bool": ("b",),
    "date": ("t",),
    "datetime": ("t",),
}
# What Decimal128(9) holds: 38 digits, 9 of them after the point.
_DECIMAL_LIMIT = Decimal(10) ** 29
_NINE_PLACES = Decimal(1).scaleb(-9)
# Room for a value that rounds up to 10^29 (39 digits), so the range check sees it.
_DECIMAL_CONTEXT = Context(prec=40)
# What DateTime64(6, 'UTC') holds.
_FIRST_INSTANT, _LAST_INSTANT = datetime(1900, 1, 1), datetime(2299, 12, 31, 23, 59, 59, 999999)

# The rejected rows a dataset names (each with its reason); the rest are counted.
MAX_REJECTED_SAMPLE = 20  # not a setting: what the catalog row describes
# The clarifications about single columns a dataset names; past them, a column's
# `inferred` type and name say the rest.
MAX_COLUMN_CLARIFICATIONS = 100  # not a setting: what the catalog row describes
_GENERATED = re.compile(r"column \d+")
# A suffixed column name's stem: room for the longest suffix, ` (16384)`.
_STEM_CHARS = MAX_COLUMN_CHARS - len(f" ({MAX_COLUMNS})")
_ROWS_FROM = re.compile(r"\d+-")  # `<name> rows 12-`: the rows from 12 on
_DURATION = re.compile(r"(-?)(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)")


def dataset_ids(source_uri: str, tables: Sequence[Table]) -> list[str]:
    """Each table's id, the same in every revision of its source: a sheet by its
    name, a CSV or Parquet file's one table by its id, and a sheet that holds more than one
    table by its name and the table's id."""
    sheets = Counter(t.locator.sheet for t in tables if t.locator.sheet)
    keys = [
        (t.locator.sheet if sheets[t.locator.sheet] == 1 else f"{t.locator.sheet}\0{t.id}")
        if t.locator.sheet
        else t.id
        for t in tables
    ]
    return ["ds_" + hashlib.sha256(f"{source_uri}\0{k}".encode()).hexdigest()[:24] for k in keys]


@dataclass(frozen=True)
class DatasetColumn:
    name: str
    type: DatasetType
    inferred: ColumnType  # what the extractor said: a DatasetType, or `mixed`
    slot: str  # a SLOT_COLUMNS name; empty when every slot of its kind is taken

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type, "inferred": self.inferred, "slot": self.slot}


@dataclass(frozen=True)
class Clarification:
    """What a person must answer before the schema is trusted: `mixed_types`,
    `generated_name`, `duplicate_name`, `header_like_data` or `past_capacity`; `column` empty when
    it is about the table (or, for `past_capacity`, all its columns of a type)."""

    code: str
    column: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "column": self.column, "detail": self.detail}


@dataclass(frozen=True)
class Rejected:
    row: int  # 1-based, among the table's rows
    column: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"row": self.row, "column": self.column, "reason": self.reason}


@dataclass(frozen=True)
class Schema:
    columns: tuple[DatasetColumn, ...]
    clarifications: tuple[Clarification, ...]


def columns_from_json(raw: list[dict[str, Any]]) -> tuple[DatasetColumn, ...]:
    """A catalog row's schema, as `DatasetColumn.to_dict` wrote it."""
    return tuple(
        DatasetColumn(
            str(c["name"]),
            cast(DatasetType, c["type"]),
            cast(ColumnType, c["inferred"]),
            str(c["slot"]),
        )
        for c in raw
    )


def infer(table: Table, overrides: dict[str, DatasetType] | None = None) -> Schema:
    """The table's schema: each column typed by `overrides` (a column name to
    its type) or else by the extractor (`mixed` as `string`), and given the
    next free slot of its type's kinds. The clarifications are what an
    override did not answer: a mixed column, a generated name, a repeated name
    (renamed by `column_names`), a header that reads as data (with no
    override: an approval answers it; not of a table whose names are no row of
    its source, `header_rows` 0), and, for each
    type, the columns no slot holds. The per-column ones stop at
    MAX_COLUMN_CLARIFICATIONS; a column's `inferred` type says the rest."""
    overrides = overrides or {}
    free = {k: list(range(n, 0, -1)) for k, n in SLOTS.items()}  # popped from the end: 1 first
    columns: list[DatasetColumn] = []
    asks: list[Clarification] = []
    unheld: dict[str, list[str]] = {}  # a type to its columns no slot holds

    def ask(code: str, column: str, detail: str) -> None:
        if len(asks) < MAX_COLUMN_CLARIFICATIONS:
            asks.append(Clarification(code, column, detail))

    for j, (c, name) in enumerate(zip(table.columns, column_names(table), strict=True)):
        # `mixed` is held as text until an approval types it.
        typed = overrides.get(name) or ("string" if c.type == "mixed" else c.type)
        kind = next((k for k in _KINDS[typed] if free[k]), "")
        slot = f"{kind}{free[kind].pop()}" if kind else ""
        columns.append(DatasetColumn(name, typed, c.type, slot))
        if not slot:
            unheld.setdefault(typed, []).append(name)
        if name in overrides:
            continue
        if name != c.name:
            ask("duplicate_name", name, f"renamed: the table repeats {c.name[:40]!r}")
        if c.type == "mixed":
            ask("mixed_types", name, _kinds_seen(table, j))
        if _GENERATED.fullmatch(c.name):
            ask("generated_name", name, "its header cell is empty")
    # A fact an approval cannot change, so it is always said: one for each type.
    for type_name, names in unheld.items():
        shown = ", ".join(n[:40] for n in names[:5]) + (", ..." if len(names) > 5 else "")
        detail = f"{len(names)} {type_name} column(s) past the slots, not stored: {shown}"
        asks.append(Clarification("past_capacity", "", detail))
    named = [c.name for c in table.columns if not _GENERATED.fullmatch(c.name)]
    # A table whose names are no row of its source (a Parquet schema) has no first row to doubt.
    if table.header_rows and not overrides and named and all(_reads_as_data(n) for n in named):
        asks.append(
            Clarification(
                "header_like_data",
                "",
                "every column name reads as a number or a date: the first row may be data",
            )
        )
    return Schema(tuple(columns), tuple(asks))


def column_names(table: Table) -> list[str]:
    """The table's column names, each unique: a repeated one gets ` (2)`, ` (3)`,
    ... after it, its text cut to make room so it stays within the contract's
    MAX_COLUMN_CHARS, as the reference extractor names them. Another extractor
    may send a header that repeats a name, and a dataset's column is addressed
    by it."""
    seen: set[str] = set()
    last: dict[str, int] = {}  # each name's last suffix, so a run of one name is linear
    out: list[str] = []
    for c in table.columns:
        name, n = c.name, last.get(c.name, 1)
        while name in seen:
            n += 1
            name = f"{c.name[:_STEM_CHARS]} ({n})"
        last[c.name] = n
        seen.add(name)
        out.append(name)
    return out


def _kinds_seen(table: Table, j: int) -> str:
    seen = sorted({_kind(r[j]) for r in table.rows} - {"null", "error"})
    return "holds " + ", ".join(seen) + " values"


def _kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, dict):
        return "error"
    return "string"


def _reads_as_data(name: str) -> bool:
    try:
        return Decimal(name.replace(",", "")).is_finite()  # `NaN`, `Inf` are words
    except InvalidOperation:
        pass
    try:
        date.fromisoformat(name)
        return True
    except ValueError:
        return False


class _Unfit(Exception):
    """A value its column's type cannot hold; the reason is the message."""


def _text(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return f"{v:.15g}"  # as the sheet shows it, as the extractor writes it
    return str(v)


def _decimal(v: Any) -> Decimal:
    if isinstance(v, bool):
        raise _Unfit("a boolean is not a number")
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            raise _Unfit("not a finite number")
        return Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        raise _Unfit(f"{str(v)[:40]!r} is not a number") from None
    if not d.is_finite():
        raise _Unfit("not a finite number")
    return d


def _seconds(v: Any) -> Decimal:
    if isinstance(v, str):
        m = _DURATION.fullmatch(v.strip())
        if m:
            sign, h, mi, s = m.groups()
            total = Decimal(h) * 3600 + Decimal(mi) * 60 + Decimal(s)
            return -total if sign else total
    raise _Unfit(f"{str(v)[:40]!r} is not a duration (H:MM:SS)")


def _number(v: Any, kind: str, typed: str) -> Decimal | float:
    d = _seconds(v) if typed == "duration" else _decimal(v)
    if kind == "f":
        f = float(d)
        if not math.isfinite(f):
            raise _Unfit("past what a number column holds")
        return f
    # Checked before rounding (a 400-digit value cannot be rounded to nine
    # places) and after (a value just under 10^29 can round up to it).
    # copy_abs, not abs(): abs() rounds to the default context's 28 digits.
    if d.copy_abs() >= _DECIMAL_LIMIT:
        raise _Unfit("past what a number column holds (10^29)")
    rounded = d.quantize(_NINE_PLACES, context=_DECIMAL_CONTEXT)
    if rounded.copy_abs() >= _DECIMAL_LIMIT:
        raise _Unfit("past what a number column holds (10^29)")
    return rounded


def _instant(v: Any, typed: str) -> datetime:
    if not isinstance(v, str):
        raise _Unfit(f"{_text(v)[:40]!r} is not a {typed}")
    text = v.strip()
    try:
        if typed == "date":
            d = date.fromisoformat(text)
            dt = datetime(d.year, d.month, d.day)
        else:
            dt = datetime.fromisoformat(text)
    except ValueError:
        raise _Unfit(f"{text[:40]!r} is not a {typed}") from None
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        except OverflowError:
            raise _Unfit(f"{text[:40]!r} is past what a {typed} column holds") from None
    if not _FIRST_INSTANT <= dt <= _LAST_INSTANT:
        raise _Unfit(f"{text[:40]!r} is outside 1900-2299, what a {typed} column holds")
    return dt


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    raise _Unfit(f"{_text(v)[:40]!r} is not a boolean")


def _time(v: Any) -> str:
    if isinstance(v, str):
        try:
            return time.fromisoformat(v.strip()).isoformat()
        except ValueError:
            pass
    raise _Unfit(f"{_text(v)[:40]!r} is not a time")


def _slot_value(v: Any, col: DatasetColumn) -> Any:
    """`v` as its column's slot holds it; raises _Unfit."""
    if v is None:
        return None
    if isinstance(v, dict):  # a cell error, `{"error": "#DIV/0!"}`
        raise _Unfit(f"cell error {str(v.get('error'))[:40]}")
    kind = col.slot[0]
    if col.type == "string":
        return _text(v)
    if col.type == "time":
        return _time(v)
    if col.type in ("number", "duration"):
        return _number(v, kind, col.type)
    if col.type == "bool":
        return _bool(v)
    return _instant(v, col.type)


def asks(clarifications: Sequence[Mapping[str, str]], *, first: int = 5) -> str:
    """The first clarifications a dataset needs answered, as a refusal names them
    (`mixed_types amount; header_like_data`)."""
    return "; ".join(f"{c['code']} {c['column']}".strip() for c in clarifications[:first])


def compared_value(v: Any, col: DatasetColumn) -> Any:
    """`v` as its column's slot holds it, to compare a stored value with. A
    number a decimal slot would round is refused, since compared rounded it is
    another number (0.0000000004 would equal 0); a number column's double slot
    rounds a value and a stored one alike. ValueError, with the reason."""
    try:
        held = _slot_value(v, col)
        exact = (
            (_seconds(v) if col.type == "duration" else _decimal(v))
            if isinstance(held, Decimal)
            else held
        )
    except _Unfit as e:
        raise ValueError(str(e)) from None
    if held != exact:
        raise ValueError(f"{_text(v)[:40]} has more than the nine decimal places a number holds")
    return held


@dataclass
class Typed:
    """A table's rows as slot values: `rows` (each a map of slot to value, in
    table order), the rows rejected (`rejected`, a count) and the first of
    them with their reason (`sample`)."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    rejected: int = 0
    sample: list[Rejected] = field(default_factory=list)


def typed_rows(table: Table, columns: tuple[DatasetColumn, ...], start: int, stop: int) -> Typed:
    """Rows `start` to `stop` (0-based, `stop` excluded) of `table` as slot
    values. A column with no slot is left out; a row one of whose values does
    not fit is rejected whole."""
    out = Typed()
    held = [(j, c) for j, c in enumerate(columns) if c.slot]
    for i in range(start, min(stop, len(table.rows))):
        r = table.rows[i]
        row: dict[str, Any] = {}
        try:
            for j, c in held:
                row[c.slot] = _slot_value(r[j], c)
        except _Unfit as e:
            out.rejected += 1
            if len(out.sample) < MAX_REJECTED_SAMPLE:
                out.sample.append(Rejected(i + 1, c.name, str(e)))
            continue
        row["row_id"] = i
        out.rows.append(row)
    return out


# A table's own failed units, as the extractor's tabular readers name them
# (`<name> rows 12-`, `<name> values`, ...), and the capabilities whose loss
# leaves a value empty or untyped, whichever sheet it is in.
_TABLE_UNITS = ("header", "merges", "values", "columns", "cells")
_VALUE_CAPABILITIES = frozenset({"formula_values", "shared_strings", "number_formats"})


def table_complete(artifact: Artifact, table: Table) -> bool:
    """Whether the extraction kept every value of `table`: no failed unit of
    the artifact is the table's (the table itself, its header, its rows past a
    budget, merges not spread, values left out, cells read twice), no shared
    string was lost (a value read empty), and the artifact lost none of the
    capabilities a value depends on (formula values, shared strings, number
    formats). A unit is matched whole, so sheet `Q1 rows` is not `Q1`'s rows."""
    name = table.name
    own = {name, "shared strings", *(f"{name} {u}" for u in _TABLE_UNITS)}
    rows = f"{name} rows "
    for f in artifact.coverage.failed:
        if f.unit in own or (f.unit.startswith(rows) and _ROWS_FROM.fullmatch(f.unit[len(rows) :])):
            return False
    return not _VALUE_CAPABILITIES.intersection(artifact.coverage.lost)


def carried(table: Table, approved: tuple[DatasetColumn, ...]) -> dict[str, DatasetType] | None:
    """An approved schema carried to a new revision of its table: each
    column's approved type, when the revision has the same column names in the
    same order (what the approval was given for); None otherwise."""
    if column_names(table) != [c.name for c in approved]:
        return None
    return {c.name: c.type for c in approved}
