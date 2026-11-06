"""A sheet's, a CSV file's or a Parquet file's rows as a table, and as the
blocks its rows are searched by: one block per run of rows (ROWS_PER_BLOCK,
BLOCK_CHARS), each row a line of `column: value` pairs, cited by its sheet and
rows, each block starting and ending at a row with text (a Parquet row whose
values were all left out has none). A header with no row of text under it is
one block of the header row, when it names a column, at row 0 when the names
are no row of the file, as a Parquet file's schema. A column's type is what
every value it holds is, over the values that are not empty or errors.

One `Budget` bounds what all of an artifact's tables hold. A source collects
a row's non-empty cells from it as it reads (`MAX_CELLS` in all, their text
within `MAX_HELD_BYTES`, `MAX_ROWS` a table), so what it holds in memory is
bounded; `table` then spends bytes from it for each row it lays out, as many as
the row writes (its values in the table, nulls included, and its line in a
block, as the artifact's JSON spells them), so the artifact stays under
`MAX_BYTES` of tables, and spends the text it indexes (`MAX_TEXT_CHARS`, each
block `CHUNK_FLOOR` more), so it stays under the server's chunk cap. The rows
past any of them are counted, never laid out, and reported as a failed unit.

Pure and I/O-free.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from loom_extractor.artifact import (
    MAX_COLUMN_CHARS,
    MAX_COLUMNS,
    Blocks,
    Column,
    ColumnType,
    Failure,
    Locator,
    Table,
)

# A block's rows and text, under a chunk, so a chunk cites the rows it holds.
ROWS_PER_BLOCK = 20  # not a setting: how a table's rows are cut
BLOCK_CHARS = 1_000  # not a setting: how a table's rows are cut
MAX_ROWS = 100_000  # not a setting: the rows a table keeps
MAX_CELLS = 1_000_000  # not a setting: the non-empty cells a source holds
MAX_BYTES = 40 * 1024 * 1024  # not a setting: what the tables write, under the 64 MiB answer
MAX_HELD_BYTES = 256 * 1024 * 1024  # not a setting: the held cells' text, four bytes a character
# The text the table blocks index, each block charged CHUNK_FLOOR more, so they
# stay under the server's default chunk cap (docs/architecture/document-extraction.md).
MAX_TEXT_CHARS = 12_000_000  # not a setting: what a source indexes
CHUNK_FLOOR = 760  # not a setting: at most the server's default chunk stride
_NON_SPACE = re.compile(r"\S")
# A suffixed column name's stem: room for the longest suffix, ` (16384)`.
_STEM_CHARS = MAX_COLUMN_CHARS - len(f" ({MAX_COLUMNS})")
# The bytes a block writes beyond its text and sheet name: its id, parent, type,
# table id, row locator and section path, keys and punctuation included.
_BLOCK_OVERHEAD = 192


@dataclass(frozen=True, slots=True)
class Cell:
    """A value and what it is: `number`, `bool`, `string`, `date`, `datetime`,
    `time`, `duration`, `error`, or `empty`."""

    value: Any
    kind: str

    @property
    def text(self) -> str:
        if self.kind == "error":
            return str(self.value["error"])
        if self.kind == "bool":
            return "TRUE" if self.value else "FALSE"
        if isinstance(self.value, float):
            return f"{self.value:.15g}"  # as a sheet shows it: 0.1+0.2 is 0.3
        return "" if self.value is None else str(self.value)


EMPTY = Cell(None, "empty")


@dataclass
class Budget:
    """What an artifact's tables may still hold."""

    cells: int = field(default_factory=lambda: MAX_CELLS)
    bytes: int = field(default_factory=lambda: MAX_BYTES)
    held: int = field(default_factory=lambda: MAX_HELD_BYTES)
    text: int = field(default_factory=lambda: MAX_TEXT_CHARS)
    # A row did not fit: no row is held after it, though what is left still
    # spreads a merge (a merged header needs a few cells).
    cut: bool = False

    @property
    def spent(self) -> bool:
        """Whether nothing more can be held or written."""
        return self.cut or self.cells <= 0 or self.bytes <= 0 or self.text < CHUNK_FLOOR

    def collect(self, cells: list[Cell]) -> bool:
        """Hold `cells`, when they fit and no row was cut before them: their
        count and their text."""
        held = 4 * sum(len(c.text) for c in cells)
        if self.cut or len(cells) > self.cells or held > self.held:
            self.cut = True  # past it once, past it for every source after
            return False
        self.cells -= len(cells)
        self.held -= held
        return True

    def spend(self, n: int) -> bool:
        if n > self.bytes:
            self.bytes = 0
            return False
        self.bytes -= n
        return True


@dataclass
class Rows:
    """A source's rows as cells, each with its 1-based row number in the source,
    and the rows past those collected (`more`, the first of them `more_from`)."""

    header: list[Cell]
    # A row is a map of column to its non-empty cells, so what it holds is what the budget counts.
    rows: list[tuple[int, dict[int, Cell]]] = field(default_factory=list)
    # The header's row in the source; 0 for a source whose names are no row of it
    # (a Parquet file's schema), whose rows count from 1.
    header_row: int = 1
    header_rows: int = 1  # the rows the header is read from, from `header_row` on
    more: int = 0
    more_from: int = 0
    first_column: int = 0  # the source's column the table starts at, from 0

    def count(self, number: int, n: int = 1) -> None:
        """Count rows `number` on (`n` of them) without holding them."""
        self.more_from = self.more_from or number
        self.more += n

    def add(self, number: int, cells: dict[int, Cell], budget: Budget) -> None:
        """Collect row `number` when the budget allows, else count it."""
        if len(self.rows) < MAX_ROWS and budget.collect(list(cells.values())):
            self.rows.append((number, cells))
        else:
            self.count(number)


def _names(header: list[Cell], width: int, first: int = 0) -> tuple[list[str], int]:
    """Column names from the header, one per column, unique and at most
    MAX_COLUMN_CHARS: an empty one is `column N`, a repeated one gets ` (2)`,
    ` (3)`, ... after it (its text cut to make room)."""
    out: list[str] = []
    seen: set[str] = set()
    cut = 0
    last: dict[str, int] = {}  # each stem's last suffix, so a run of one name is linear
    # Each distinct cell read once: a merged header is one cell over many columns.
    read: dict[int, tuple[str, bool]] = {}
    for i in range(width):
        cell = header[i] if i < len(header) else EMPTY
        hit = read.get(id(cell))
        if hit is None:
            # Cut before the spaces are folded: a header cell may hold a megabyte.
            folded = " ".join(cell.text[: 2 * MAX_COLUMN_CHARS + 1].split())
            tail = _NON_SPACE.search(cell.text, 2 * MAX_COLUMN_CHARS + 1) is not None
            hit = read[id(cell)] = folded, tail
        folded, tail = hit
        base = folded[:MAX_COLUMN_CHARS] or f"column {first + i + 1}"  # as the source counts
        name = base
        # Cut when the folded name is too long, or text lies past what was read of the cell.
        was_cut = len(folded) > MAX_COLUMN_CHARS or tail
        if name in seen:
            stem = base[:_STEM_CHARS]
            was_cut = was_cut or len(base) > _STEM_CHARS  # a suffix cuts the stem
            n = last.get(stem, 1)
            while name in seen:
                n += 1
                name = f"{stem} ({n})"
            last[stem] = n
        cut += was_cut
        seen.add(name)
        out.append(name)
    return out, cut


def _type(cells: list[Cell]) -> ColumnType:
    kinds = {c.kind for c in cells} - {"empty", "error"}
    if not kinds:
        return "string"
    if len(kinds) == 1:
        (only,) = kinds
        return only  # type: ignore[return-value]  # one of the column types
    return "datetime" if kinds == {"date", "datetime"} else "mixed"


def _line(names: list[str], cells: list[Cell]) -> str:
    return "; ".join(f"{names[i]}: {c.text}" for i, c in enumerate(cells) if c.kind != "empty")


def _size(value: Any) -> int:
    """The bytes `value` writes in the artifact's JSON, and the separator after it."""
    return len(json.dumps(value)) + 1


def _row_size(names: list[str], cells: list[Cell], left: int) -> int:
    """At least what a row writes (its values and its block line, as `_size`
    counts them), measured a cell at a time so a row is never spelled out
    whole; past `left`, a number over it."""
    total = 6  # the brackets, the quotes and the separators after them
    for name, c in zip(names, cells, strict=True):
        total += _size(c.value)  # the value and the comma after it
        if c.kind != "empty":
            # Its part of the line, escaped: the part's own quotes are the room for "; ".
            total += len(json.dumps(f"{name}: {c.text}"))
        if total > left:
            break
    return total


def counted(n: int, noun: str) -> str:
    """`1 row`, `2 rows`: a failure's count and what it counts."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def fail(budget: Budget, failed: list[Failure], unit: str, reason: str) -> None:
    """Record a failed unit, its reason cut, spending what it writes: past the
    budget it is still recorded, and what follows is not read."""
    failure = Failure(unit, reason[:200])
    budget.spend(_size(unit) + _size(failure.reason) + 30)
    failed.append(failure)


def heading(budget: Budget, sheet: str) -> bool:
    """Whether a sheet's heading block (its name, as text, section path and
    locator) fits the budget; if so it is spent."""
    return budget.spend(_BLOCK_OVERHEAD + 3 * _size(sheet))


def table(
    acc: Blocks, tid: str, name: str, rows: Rows, budget: Budget, *, sheet: str = ""
) -> tuple[Table | None, list[Failure]]:
    """`rows` as table `tid`, its row blocks added to `acc`, as far as `budget`
    lets it write: the table (None when not even its header fits), and its
    failed units (rows past the budget). A source leaves out the values past
    MAX_COLUMNS itself, and counts them."""
    failed: list[Failure] = []
    width = max([len(rows.header), *(max(r) + 1 for _, r in rows.rows if r)], default=0)
    names, cut = _names(rows.header, width, rows.first_column)
    if cut:  # the header's text past the names a table holds is not in the artifact
        reason = f"{counted(cut, 'column name')} cut to {MAX_COLUMN_CHARS} characters"
        fail(budget, failed, f"{name} header", reason)
    header = rows.header[:width]
    # The names in the table (each with its type), and as a block of their own
    # when no row follows (the names, cut to MAX_COLUMN_CHARS, not the cells' text).
    head = "; ".join(names[i] for i, c in enumerate(header) if c.kind != "empty")
    # Each block names the sheet twice (its locator and its section path).
    per_block = _BLOCK_OVERHEAD + 2 * _size(sheet)
    # The table writes its name and its sheet's in its locator too.
    own = _size(name) + 2 * _size(sheet) + per_block
    if not budget.spend(sum(_size(n) + 30 for n in names) + _size(head) + own):
        fail(budget, failed, name, "its header is past what one artifact holds")
        return None, failed
    grid: list[tuple[int, list[Cell]]] = []
    blocks: list[list[tuple[int, str]]] = []  # each block's rows: (row number, line)
    chars = 0  # the open block's text
    more, more_from = rows.more, rows.more_from
    for k, (n, r) in enumerate(rows.rows):
        cells = [r.get(i, EMPTY) for i in range(width)]
        fits = budget.spend(_row_size(names, cells, budget.bytes))
        line = _line(names, cells) if fits else ""
        opens = not blocks or len(blocks[-1]) == ROWS_PER_BLOCK or chars + len(line) > BLOCK_CHARS
        if fits and opens:
            fits = budget.spend(per_block)  # a new block's own bytes
        text = len(line) + 1 + (CHUNK_FLOOR if opens else 0)
        if not fits or text > budget.text:
            more, more_from = more + len(rows.rows) - k, n
            break
        budget.text -= text
        if opens:
            blocks.append([])
            chars = 0
        blocks[-1].append((n, line))
        chars += len(line) + 1
        grid.append((n, cells))
    if more:
        reason = f"{counted(more, 'row')} past what one artifact holds"
        fail(budget, failed, f"{name} rows {more_from}-", reason)
    columns = tuple(Column(names[i], _type([r[i] for _, r in grid])) for i in range(width))
    written = False
    for run in blocks:
        # A row whose values were all left out has no line: the block holds none
        # of it, and starts and ends at a row with text.
        lines = [(n, line) for n, line in run if line]
        if lines:
            loc = Locator(sheet=sheet, row_start=lines[0][0], row_end=lines[-1][0])
            acc.add("paragraph", "\n".join(line for _, line in lines), loc, table_id=tid)
            written = True
    if not written and head:
        # A header with nothing under it to read (a sheet of one note, a Parquet
        # column of bytes) is still text to find; one of no names is no text.
        if len(head) + CHUNK_FLOOR > budget.text:
            fail(budget, failed, name, "its header is past what one artifact holds")
            return None, failed
        budget.text -= len(head) + CHUNK_FLOOR
        end = rows.header_row + rows.header_rows - 1
        loc = Locator(sheet=sheet, row_start=rows.header_row, row_end=end)
        acc.add("paragraph", head, loc, table_id=tid)
    # The rows the table came from: its header row, or with none, its first row.
    first = rows.header_row or (grid[0][0] if grid else 0)
    last = grid[-1][0] if grid else rows.header_row
    return (
        Table(
            id=tid,
            name=name,
            columns=columns,
            rows=tuple(tuple(c.value for c in r) for _, r in grid),
            header_rows=rows.header_rows if rows.header_row else 0,
            locator=Locator(sheet=sheet, row_start=first, row_end=last),
        ),
        failed,
    )
