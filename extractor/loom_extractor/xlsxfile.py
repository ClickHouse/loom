"""An XLSX workbook as tables, read from its XML parts with the standard library:
each worksheet in the workbook's order is a table, its first row with a value
its header, the rows under it its values. A cell reads as the workbook holds it:
a shared or inline string, a number, a bool, an error (`{"error": "#DIV/0!"}`),
a date when its number format is one (the 1900 date system, Excel's 1900-02-29
read as text, or 1904 when the workbook says so), a time of day when the format
holds a time and no date, or a duration when it is an elapsed-time format
(`[h]:mm`). A value over MAX_VALUE_CHARS is left out and counted. A formula
reads its cached value; one with none (a workbook saved without calculating)
reads empty, and the artifact loses `formula_values`. A merged range takes its
first cell's value in every cell, so a header merged over columns names each of
them, as far as the artifact's budget of cells allows; a range merged across
columns of the header row over text cells in the row under it makes a header of
both rows (`2025 / H1`), as long as no other cell of that row is its own: one
that is makes the row data, and a title merged over the whole header row heads
every name. A row or cell that names no position (`r`) takes the one after the
row or cell before it.

The rows are blocks too, cited by sheet and rows (`tabular`).

Pure and I/O-free.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import date, datetime, timedelta

from loom_extractor.artifact import (
    MAX_COLUMNS,
    MAX_SECTION_CHARS,
    Artifact,
    Blocks,
    Coverage,
    Failure,
    Locator,
    Table,
    on_python,
)
from loom_extractor.ooxml import Cut, Exhausted, Package, PartError, q
from loom_extractor.tabular import (
    EMPTY,
    MAX_ROWS,
    NAME_READ_CHARS,
    Budget,
    Cell,
    Rows,
    counted,
    fail,
    heading,
    table,
)

PARSER_VERSION = "2"
_WORKBOOK = "xl/workbook.xml"
_CELL_REF = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
_RANGE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6}):([A-Z]{1,3})([1-9][0-9]{0,6})")
_INDEX = re.compile(r"[0-9]{1,9}")  # a shared-string, style or row index
_HAS_TEXT = re.compile(r"\S")
_INTEGER = re.compile(r"-?[0-9]{1,19}")  # exact as written, within signed 64 bits
# Built-in date and time-of-day formats, East Asian Excel's included.
_DATE_FORMATS = {14, 15, 16, 17, 22, *range(27, 32), 36, *range(50, 59)}
_TIME_FORMATS = {18, 19, 20, 21, 45, 47, *range(32, 36)}
_ELAPSED_FORMATS = {46}  # [h]:mm:ss: a duration, hours past a day included
_MAX_DURATION_DAYS = 10**7  # a longer elapsed time stays a number
_ELAPSED = re.compile(r"\[[hms]+\]", re.IGNORECASE)
# Quoted text, escapes and brackets (a colour, a locale) are no date part; an
# elapsed-time bracket (`[h]`) is. Optional closers keep an unclosed one linear.
_QUOTED = re.compile(r'"[^"]*"?|\[(?![hms]+\])[^\]]*\]?|\\.', re.IGNORECASE)
_DATE_CODE = re.compile(r"[yd]", re.IGNORECASE)
_TIME_CODE = re.compile(r"[hs]", re.IGNORECASE)
_LAST_DAY = datetime(9999, 12, 31)
_ROW, _C, _V, _F, _IS, _T, _RPH, _MERGE, _SI = (
    q(f"x:{t}") for t in ("row", "c", "v", "f", "is", "t", "rPh", "mergeCell", "si")
)
_NUMFMT, _CELLXFS, _XF, _SHEET, _WORKBOOK_PR, _SHEET_DATA = (
    q(f"x:{t}") for t in ("numFmt", "cellXfs", "xf", "sheet", "workbookPr", "sheetData")
)
_RID = q("r:id")[1:]  # as expat spells the attribute
_MERGE_CELLS = q("x:mergeCells")
# The worksheet's parts the format puts after its merged ranges (ECMA-376's
# CT_Worksheet order): a cut in one of them loses nothing a table holds.
_PAST_MERGES = {
    q(f"x:{t}")
    for t in (
        "phoneticPr",
        "conditionalFormatting",
        "dataValidations",
        "hyperlinks",
        "printOptions",
        "pageMargins",
        "pageSetup",
        "headerFooter",
        "rowBreaks",
        "colBreaks",
        "customProperties",
        "cellWatches",
        "ignoredErrors",
        "smartTags",
        "drawing",
        "legacyDrawing",
        "legacyDrawingHF",
        "drawingHF",
        "picture",
        "oleObjects",
        "controls",
        "webPublishItems",
        "tableParts",
        "extLst",
    )
}
_WORKBOOK_ROOT = q("x:workbook")
# A part's root element: one in another namespace holds none of the elements read.
_WORKSHEET_ROOT, _STYLES_ROOT, _SHARED_ROOT = (
    q(f"x:{t}") for t in ("worksheet", "styleSheet", "sst")
)


# Strict Open XML names its parts' elements in namespaces of its own, which this
# reader does not take: refused, not read as an empty workbook.
_STRICT_WORKBOOK = "{http://purl.oclc.org/ooxml/spreadsheetml/main}workbook"
# Shared strings held, each item at four bytes a character plus its object.
MAX_SHARED_BYTES = 256 * 1024 * 1024  # not a setting: a bound on hostile input
_ITEM_BYTES = 96
MAX_STYLES = 1 << 16  # not a setting: styles and number formats read (Excel holds 64,000)
MAX_SHEETS = 10_000  # not a setting: a bound on hostile input
MAX_MERGES = 1_000_000  # not a setting: merged ranges a workbook reads, a bound on their time
_RELS_BYTES = 4 * 1024 * 1024  # the workbook's relationships, read whole
MAX_VALUE_CHARS = 1 << 20  # not a setting: a value a table holds, as a CSV field
# Excel's 1900 system counts a 1900-02-29 that never was: serial 60. Serials
# before it count from 1899-12-31, the rest from 1899-12-30.
_EPOCH_1900, _EPOCH_1900_EARLY = datetime(1899, 12, 30), datetime(1899, 12, 31)
_EPOCH_1904 = datetime(1904, 1, 1)


class StrictWorkbook(Exception):
    """A workbook saved as Strict Open XML."""


def _string(text: str) -> Cell:
    """A string cell; one of only whitespace (a space typed to clear a cell) is
    empty, as a CSV field is."""
    return Cell(text, "string") if _HAS_TEXT.search(text) else EMPTY


def _rooted(tag: str, root: str, what: str) -> None:
    if tag != root:
        raise PartError(f"the {what} part is no {what}: {tag[:100]}")


def _column(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _code_kind(code: str) -> str:
    code = _QUOTED.sub("", code)
    if _ELAPSED.search(code):
        return "duration"
    if _DATE_CODE.search(code) or ("m" in code.lower() and not _TIME_CODE.search(code)):
        return "date"  # `m` alone is a month; with h or s, minutes
    return "time" if _TIME_CODE.search(code) else ""


class _StylesStream:
    """`xl/styles.xml` as expat reads it: the custom number formats, and the
    style indexes (`s` on a cell, a `cellXfs` entry) whose format is a `date`, a
    `time` of day or a `duration` (an elapsed-time code, `[h]:mm`), up to
    MAX_STYLES of each (`cut` when there were more). A custom format is declared
    before the styles use it."""

    def __init__(self) -> None:
        self.custom: dict[int, str] = {}  # by id: an id that is no index is not held
        self.kinds: dict[int, str] = {}
        self._xfs = -1  # the index of the last cellXfs entry read, -1 before the first
        self._numfmts = 0
        self._in_xfs = False
        self.cut = False  # styles or formats past MAX_STYLES, read as numbers
        self._open = False  # past the root element

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if not self._open:
            self._open = True
            _rooted(tag, _STYLES_ROOT, "styles")
        if tag == _NUMFMT and self._numfmts >= MAX_STYLES:
            self.cut = True
        elif tag == _NUMFMT:  # every format scanned is counted
            self._numfmts += 1
            fid = attrs.get("numFmtId", "")
            # Excel writes a format code in at most 255 characters; past that, it is no format.
            code = attrs.get("formatCode", "")
            kind = _code_kind(code) if _INDEX.fullmatch(fid) and len(code) <= 255 else ""
            if kind:
                self.custom[int(fid)] = kind
        elif tag == _CELLXFS:
            self._in_xfs = True
        elif tag == _XF and self._in_xfs and self._xfs + 1 >= MAX_STYLES:
            # Nothing after the styles is used: the rest is not read, and spends
            # none of the element bound the sheets need.
            self.cut = True
            raise Cut(f"styles past {MAX_STYLES}")
        elif tag == _XF and self._in_xfs:
            self._xfs += 1
            fid = attrs.get("numFmtId", "0")
            n = int(fid) if _INDEX.fullmatch(fid) else -1
            if n in _DATE_FORMATS:
                kind = "date"
            elif n in _ELAPSED_FORMATS:
                kind = "duration"
            elif n in _TIME_FORMATS:
                kind = "time"
            else:
                kind = self.custom.get(n, "")
            if kind:
                self.kinds[self._xfs] = kind

    def data(self, text: str) -> None:
        pass

    def end(self, tag: str) -> None:
        if tag == _CELLXFS:
            self._in_xfs = False


def _formats(pkg: Package, part: str) -> tuple[dict[int, str], bool]:
    """The style indexes whose number format is a date, a time or a duration,
    read from the workbook's styles part ("" when it names none), and whether
    any were not read (the part read no further, or styles past MAX_STYLES:
    those read before stand; the rest read as numbers)."""
    styles = _StylesStream()
    if part:
        try:
            pkg.stream(part, styles)
        except PartError:
            return styles.kinds, True
    return styles.kinds, styles.cut


class _SharedStream:
    """`xl/sharedStrings.xml` as expat reads it: each string item's text, or its
    rich text runs' text (a phonetic reading, `rPh`, is not its text), as far as
    MAX_SHARED_BYTES in all: past it, the rest of the part is not read (`Cut`).
    An item over MAX_VALUE_CHARS reads empty, and is counted (`long`)."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.cut = False
        self.long = 0
        self._left = MAX_SHARED_BYTES
        self._parts: list[str] = []
        self._held = 0  # the text of the item being read
        self._in_t = False
        self._phonetic = 0
        self._open = False  # past the root element

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if not self._open:
            self._open = True
            _rooted(tag, _SHARED_ROOT, "shared strings")
        if tag == _SI:
            self._parts, self._held = [], 0
        elif tag == _RPH:
            self._phonetic += 1
        elif tag == _T:
            self._in_t = not self._phonetic

    def data(self, text: str) -> None:
        if self._in_t:
            self._held += len(text)
            if self._held <= MAX_VALUE_CHARS:  # past it, no more of the item is held
                self._parts.append(text)

    def end(self, tag: str) -> None:
        if tag == _T:
            self._in_t = False
        elif tag == _RPH:
            self._phonetic -= 1
        elif tag == _SI:
            text = "" if self._held > MAX_VALUE_CHARS else "".join(self._parts)
            self.long += self._held > MAX_VALUE_CHARS
            self._left -= _ITEM_BYTES + 4 * len(text)
            if self._left < 0:  # past the bound: the rest of the part is not read
                self.cut = True
                raise Cut(f"shared strings past {MAX_SHARED_BYTES} bytes held")
            self.items.append(text)


def _shared(pkg: Package, part: str) -> _SharedStream:
    """The workbook's shared strings, read from the part it names ("" when it
    names none), as far as they are held."""
    shared = _SharedStream()
    if part:
        try:
            pkg.stream(part, shared)
        except PartError:  # damaged, over a bound: the items read before stand
            shared.cut = True
    return shared


def _iso(raw: str) -> Cell:
    """A `t="d"` cell's ISO text: a date, a date and time, or text when it is neither."""
    try:
        return Cell(date.fromisoformat(raw).isoformat(), "date")
    except ValueError:
        pass
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return _string(raw)
    # To the nearest second, its offset kept, as a serial reads.
    try:
        when = (when + timedelta(microseconds=500_000)).replace(microsecond=0)
    except OverflowError:  # past 9999-12-31T23:59:59
        return Cell(raw, "string")
    return Cell(when.isoformat(), "datetime")


class _Reader:
    def __init__(self, shared: list[str], formats: dict[int, str], date1904: bool) -> None:
        self.shared, self.formats, self.date1904 = shared, formats, date1904
        self.uncalculated = 0  # formulas with no cached value
        self.unshared = 0  # shared-string cells naming no string held
        self.merges_read = 0  # mergeCell elements read in the workbook, its sheets together
        self.exhausted = ""  # why the package's bounds stopped the reading, if they did

    def _serial(self, serial: float, kind: str) -> Cell:
        if kind == "duration" and 0 <= serial <= _MAX_DURATION_DAYS:
            seconds = round(serial * 86_400)  # elapsed time: hours past a day
            return Cell(
                f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}", "duration"
            )
        if kind == "time" and 0 <= serial <= _MAX_DURATION_DAYS:
            # Its time of day, whatever day it is (`=NOW()` shown as `h:mm`).
            seconds = round(serial * 86_400) % 86_400
            hms = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
            return Cell(hms, "time")
        if self.date1904:
            epoch = _EPOCH_1904
        elif serial < 60:
            epoch = _EPOCH_1900_EARLY
        elif serial < 61:
            return Cell("1900-02-29", "string")  # the day Excel counts and no calendar has
        else:
            epoch = _EPOCH_1900
        if not 0 <= serial < (_LAST_DAY - epoch).days + 1:  # 9999-12-31 and any time on it
            return Cell(serial, "number")
        # To the nearest second: a serial written to 15 digits lands a hair under it.
        try:
            when = epoch + timedelta(seconds=round(serial * 86_400))
        except OverflowError:  # a time on 9999-12-31 that rounds into the year 10000
            return Cell(serial, "number")
        if when.time() == datetime.min.time() and serial == int(serial):
            return Cell(when.date().isoformat(), "date")
        return Cell(when.isoformat(timespec="seconds"), "datetime")

    def cell(self, kind: str, style: str, raw: str | None, formula: bool, inline: str) -> Cell:
        """A `<c>` as its value: its type (`t`), style (`s`), `<v>` text (None
        when it has none), whether it has a formula, and its inline string."""
        if kind == "inlineStr":
            return _string(inline)
        if not raw:  # no `<v>`, or an empty one
            if formula and raw is None:
                self.uncalculated += 1
            return EMPTY
        if kind == "s":
            i = int(raw) if _INDEX.fullmatch(raw) else -1
            if not 0 <= i < len(self.shared):
                self.unshared += 1  # a string the workbook does not hold: its text is lost
                return EMPTY
            return _string(self.shared[i])
        if kind == "str":
            return _string(raw)
        if kind == "d":
            return _iso(raw)
        if kind == "b":
            return Cell(raw == "1", "bool")
        if kind == "e":
            return Cell({"error": raw}, "error")
        try:
            number = float(raw)
        except ValueError:
            return _string(raw)
        if not math.isfinite(number):  # no JSON number
            return Cell(raw, "string")
        fmt = self.formats.get(int(style)) if _INDEX.fullmatch(style) else None
        if fmt:
            return self._serial(number, fmt)
        if _INTEGER.fullmatch(raw) and -(2**63) <= int(raw) < 2**63:
            return Cell(int(raw), "number")
        return Cell(number, "number")

    def rows(
        self, pkg: Package, name: str, part: str, budget: Budget
    ) -> tuple[Rows | None, list[Failure], int]:
        """Sheet `name` read as its part inflates: its rows (None when none was
        held), its failed units, and the rows it lost that the `Rows` do not
        count (the rest of a cut sheet is one)."""
        sheet = _SheetStream(self, budget)
        failed = []
        unread = 0
        merges_cut = ""
        try:
            pkg.stream(part, sheet)
        except Cut as e:
            if isinstance(e, Exhausted):  # no part after it is read
                budget.cut = True
                self.exhausted = str(e)
            # After every row and merge, nothing a table holds is lost.
            if not (sheet.rows_done and sheet.merges_done):
                if sheet.rows_done:  # cut in the merged ranges, after every row
                    merges_cut = str(e)
                else:  # the row it ran out in was not collected
                    unread += 1
                    unit = f"{name} rows {sheet.number + (not sheet.open)}-"
                    failed.append(Failure(unit, str(e)))
        # A merge reaches no row past the last one held (nor, in `_spread`, past MAX_ROWS).
        rest = sheet.past
        last = rest.more_from - 1 if rest.more_from else max(sheet.grid, default=0)
        skipped, clipped = _spread(sheet.grid, sorted(sheet.merges), budget, last)
        skipped += sheet.merges_dropped
        if skipped or clipped or merges_cut:  # one unit for the sheet's merges, its reasons joined
            reasons = (
                merges_cut,
                f"{counted(skipped, 'merged range')} not spread" if skipped else "",
                f"{counted(clipped, 'merged range')} cut at the row cap" if clipped else "",
            )
            reason = "; ".join(r for r in reasons if r)
            failed.append(Failure(f"{name} merges", reason))
        if sheet.long:
            reason = f"{counted(sheet.long, 'value')} over {MAX_VALUE_CHARS} characters"
            failed.append(Failure(f"{name} values", reason))
        if sheet.wide:
            reason = f"{counted(sheet.wide, 'value')} past column {MAX_COLUMNS}"
            failed.append(Failure(f"{name} columns", reason))
        if sheet.repeated:
            reason = f"{counted(sheet.repeated, 'cell')} at a position already read"
            failed.append(Failure(f"{name} cells", reason))
        if not sheet.grid:
            if rest.more:  # not even its first row could be held
                reason = f"{counted(rest.more, 'row')} past what one artifact holds"
                failed.append(Failure(f"{name} rows {rest.more_from}-", reason))
                unread += rest.more - 1  # the rows under its header, as a sheet read counts
            return None, failed, unread
        left = min(col for row in sheet.grid.values() for col in row)
        numbers = sorted(sheet.grid)
        right = max(col for row in sheet.grid.values() for col in row)
        top = numbers[0]
        header = [sheet.grid[top].get(col, EMPTY) for col in range(left, right + 1)]
        # A header over two rows: a range merged across columns of the first row
        # groups the names the row under it gives (`2025` over `H1` and `H2`), when
        # that row holds text under the range and, elsewhere, nothing or only cells
        # merged down from the first row; a row of anything else is data.
        spans = [(c, r) for t, c, b, r in sheet.merges if t == b == top and r > c]
        above, under = sheet.grid[top], sheet.grid.get(top + 1, {})
        # The second row's own cells: those not merged down from the first.
        own = {col: cell for col, cell in under.items() if cell is not above.get(col)}
        grouped = bool(own) and all(
            cell.kind == "string" and any(c <= col <= r for c, r in spans)
            for col, cell in own.items()
        )
        if grouped:
            header = [
                _named_over(above.get(col, EMPTY), under.get(col, EMPTY))
                for col in range(left, right + 1)
            ]
        out = Rows(
            header,
            header_row=top,
            header_rows=2 if grouped else 1,
            more=rest.more,
            more_from=rest.more_from,
            first_column=left,
        )
        values = numbers[2:] if grouped else numbers[1:]
        out.rows = [(n, {col - left: c for col, c in sheet.grid[n].items()}) for n in values]
        return out, failed, unread


class _SheetStream:
    """A worksheet part's cells as expat reads them: each row's non-empty cells
    collected from the budget when the row ends (the rows past it counted), and
    its merged ranges."""

    def __init__(self, reader: _Reader, budget: Budget) -> None:
        self.reader, self.budget = reader, budget
        self.grid: dict[int, dict[int, Cell]] = {}
        # Each range as (top, left, bottom, right); one repeated is spread once.
        self.merges: set[tuple[int, int, int, int]] = set()
        self.merges_dropped = 0  # past the cells left, or past MAX_MERGES: not spread
        self.long = 0  # values over MAX_VALUE_CHARS, left out
        self.wide = 0  # values past MAX_COLUMNS, left out
        self.repeated = 0  # cells at a position already read: the earlier value is lost
        self._chars = 0  # the value being read
        self._too_long = False
        self.past = Rows([])  # the rows past the budget, counted by the table's rule
        self._filled = False  # the row has a value (counted, past the budget)
        self.number = 0
        self.open = False  # within a row
        self.rows_done = False  # past `sheetData`
        self.merges_done = False  # past `mergeCells`, or where it would be
        self._depth = 0  # of the element being read: `worksheet` is 1
        self._row_held = 0  # what the open row's text takes, as the budget counts it
        self._row: dict[int, Cell] = {}
        self._col = -1
        self._cell: dict[str, str] = {}
        self._v: list[str] | None = None
        self._raw: str | None = None
        self._formula = False
        self._inline: list[str] = []
        self._in_is = self._in_t = False
        self._phonetic = 0

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self._depth += 1
        if self._depth == 1:  # the root
            _rooted(tag, _WORKSHEET_ROOT, "worksheet")
        # A part the format puts after the merges is a child of `worksheet`: the same
        # names sit inside a view, a filter or a row too.
        if self.rows_done and self._depth == 2 and tag in _PAST_MERGES:
            self.merges_done = True
        elif tag == _MERGE_CELLS:  # merges out of the format's order are still merges
            self.merges_done = False
        elif tag == _SHEET_DATA:  # a second one is rows still to read
            self.rows_done = False
        if tag == _C:
            m = _CELL_REF.fullmatch(attrs.get("r", ""))
            self._col = _column(m.group(1)) if m is not None else self._col + 1
            self._cell, self._raw, self._formula, self._inline = attrs, None, False, []
            self._chars, self._too_long = 0, False
        elif tag == _V:
            self._v = []
        elif tag == _F:
            self._formula = True
        elif tag == _IS:
            self._in_is = True
        elif tag == _RPH:
            self._phonetic += 1
        elif tag == _T:
            self._in_t = self._in_is and not self._phonetic
        elif tag == _ROW:
            r = attrs.get("r", "")
            self.number = int(r) if _INDEX.fullmatch(r) and int(r) > 0 else self.number + 1
            self._row, self._col, self._filled, self.open = {}, -1, False, True
            self._row_held = 0
        elif tag == _MERGE:
            # A workbook reads at most MAX_MERGES ranges, a bound on the time they take:
            # past it each one is counted without being read (a repeat or no range too).
            self.reader.merges_read += 1
            if self.reader.merges_read > MAX_MERGES:
                self.merges_dropped += 1
                return
            m = _RANGE.fullmatch(attrs.get("ref", ""))
            if m is None:  # no range: it spreads nothing
                return
            box = (int(m[2]), _column(m[1]), int(m[4]), _column(m[3]))
            if box in self.merges:
                return
            if len(self.merges) < self.budget.cells:
                self.merges.add(box)
            else:
                self.merges_dropped += 1

    def data(self, text: str) -> None:
        if self._v is None and not self._in_t:
            return
        self._chars += len(text)
        if self._chars > MAX_VALUE_CHARS:  # held no further: the value is left out
            self._too_long = True
        elif self._v is not None:
            self._v.append(text)
        else:
            self._inline.append(text)

    def end(self, tag: str) -> None:
        self._depth -= 1
        if tag == _V and self._v is not None:
            self._raw, self._v = "".join(self._v), None
        elif tag == _T:
            self._in_t = False
        elif tag == _RPH:
            self._phonetic -= 1
        elif tag == _IS:
            self._in_is = False
        elif tag == _C:
            if self._too_long:
                self.long += 1
                return
            if self._col >= MAX_COLUMNS:
                self.wide += self._has_value()
                return
            if self._full():  # past the budget: whether the row has a value, not what it is
                self._filled = self._filled or self._has_value()
                return
            a = self._cell
            cell = self.reader.cell(
                a.get("t", "n"), a.get("s", "0"), self._raw, self._formula, "".join(self._inline)
            )
            if cell.kind != "empty":
                self.repeated += self._col in self._row  # a position written twice: one value lost
                self._row[self._col] = cell
                self._row_held += 4 * len(cell.text)
                if self._row_held > self.budget.held:  # the row cannot be held: counted, not read
                    self._row, self._filled = {}, True
        elif tag == _SHEET_DATA:
            self.rows_done = True
        elif tag == _MERGE_CELLS:
            self.merges_done = True
        elif tag == _ROW:
            self.open = False
            if not (self._row or self._filled):
                return
            # The header and up to MAX_ROWS rows under it, as the budget allows.
            if self._row and not self._full() and self.budget.collect(list(self._row.values())):
                held = self.grid.setdefault(self.number, {})
                self.repeated += len(held.keys() & self._row.keys())
                held.update(self._row)
            else:
                self.past.count(self.number)

    def _has_value(self) -> bool:
        """Whether the cell being read holds a value, without typing it, by the
        rules `_Reader.cell` reads it by: text of only whitespace is none, a bool or
        an error is one, and so is a formula with no cached value (it is unknown)."""
        kind = self._cell.get("t", "n")
        if kind == "inlineStr":
            return any(_HAS_TEXT.search(t) for t in self._inline)
        if self._raw is None:
            return self._formula
        if not self._raw:
            return False
        if kind in ("b", "e"):
            return True
        if kind == "s":
            i = int(self._raw) if _INDEX.fullmatch(self._raw) else -1
            return not 0 <= i < len(self.reader.shared) or bool(
                _HAS_TEXT.search(self.reader.shared[i])
            )  # a string not held is lost text: a value
        return bool(_HAS_TEXT.search(self._raw))

    def _full(self) -> bool:
        """Whether the open row is past the budget: only checked for a value."""
        over = self._row_held > self.budget.held
        return over or bool(self.past.more) or len(self.grid) > MAX_ROWS or self.budget.cut


def _named_over(above: Cell, below: Cell) -> Cell:
    """A column's name from a header over two rows: the first row's cell, the
    second's under it joined after ` / `; one merged down both rows (the same
    cell) or with nothing under it is its own name, and one with nothing above
    the second's."""
    if below is above or below.kind == "empty":
        return above
    if above.kind == "empty":
        return below

    # Each as its name reads it, folded, not a cell's megabyte for each column it spans.
    def name(cell: Cell) -> str:
        return " ".join(cell.text[:NAME_READ_CHARS].split())

    return Cell(f"{name(above)} / {name(below)}", "string")


def _spread(
    grid: dict[int, dict[int, Cell]],
    boxes: list[tuple[int, int, int, int]],
    budget: Budget,
    last: int,
) -> tuple[int, int]:
    """Each merged range's first cell's value in each of its cells up to row
    `last`, the cells it adds collected from `budget`; the ranges skipped (past
    the cells left) and those cut at the MAX_ROWS under the header, counted."""
    skipped = clipped = 0
    for top, left, bottom, right in boxes:
        bottom, right = min(bottom, last), min(right, MAX_COLUMNS - 1)
        value = grid.get(top, {}).get(left)
        if value is None or bottom < top or right < left:
            continue
        # One too large for the cells left is skipped, leaving them to the rest.
        area = (bottom - top + 1) * (right - left + 1)
        if area > budget.cells:
            skipped += 1
            continue
        budget.cells -= area
        for r in range(top, bottom + 1):
            if r not in grid and len(grid) > MAX_ROWS:  # past the rows a table holds
                clipped += 1
                budget.cells += (bottom - r + 1) * (right - left + 1)  # the rows not filled
                break
            row = grid.setdefault(r, {})
            for col in range(left, right + 1):
                row.setdefault(col, value)
    return skipped, clipped


class _WorkbookStream:
    """`xl/workbook.xml` as expat reads it: its first MAX_SHEETS sheets' (name,
    relationship id, hidden), the rest counted (`more`), and whether it counts
    dates from 1904."""

    def __init__(self) -> None:
        self.sheets: list[tuple[str, str, bool]] = []
        self.more = 0
        self.date1904 = False
        self.cut = ""  # why the part was read no further
        self.names_cut = 0  # sheet names cut to the contract
        self._open = False  # past the root element

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if not self._open:
            self._open = True
            if tag == _STRICT_WORKBOOK:
                raise StrictWorkbook("a Strict Open XML workbook; save it as an Excel workbook")
            if tag != _WORKBOOK_ROOT:
                raise PartError(f"{_WORKBOOK} is no workbook: {tag[:100]}")
        if tag == _WORKBOOK_PR:
            self.date1904 = attrs.get("date1904", "") in ("1", "true")
        elif tag == _SHEET and len(self.sheets) < MAX_SHEETS:
            # Excel names a sheet in at most 31 characters; a longer name is cut to the contract.
            full = attrs.get("name", "")
            name = full[:MAX_SECTION_CHARS]
            self.names_cut += len(full) > MAX_SECTION_CHARS
            hidden = attrs.get("state", "visible") != "visible"
            self.sheets.append((name, attrs.get(_RID, "")[:255], hidden))
        elif tag == _SHEET:
            self.more += 1

    def data(self, text: str) -> None:
        pass

    def end(self, tag: str) -> None:
        pass


def _sheets(
    pkg: Package, rels: dict[str, tuple[str, str]]
) -> tuple[list[tuple[int, str, str, bool, str]], _WorkbookStream]:
    """Each sheet that may hold cells (position in the workbook, from 1; name;
    part, "" when it names no worksheet; hidden; its relationship's type) in the
    workbook's order, and the workbook part as read (the sheets it names, its
    date system, its cut)."""
    book = _WorkbookStream()
    try:
        pkg.stream(_WORKBOOK, book)
    except Cut as e:  # the sheets named before it are read; those after it are unknown
        book.cut = str(e)
    out = []
    for pos, (name, rid, hidden) in enumerate(book.sheets, 1):
        kind, target = rels.get(rid, ("", ""))
        if kind in ("chartsheet", "dialogsheet"):  # holds no cells
            continue
        # A worksheet, or a sheet this reader does not read (a macro sheet holds
        # cells): the latter, as a relationship naming none, fails alone.
        out.append((pos, name, target if kind == "worksheet" else "", hidden, kind))
    return out, book


def extract_xlsx(data: bytes) -> Artifact:
    pkg = Package(data)
    # Its relationships first: a workbook part cut at the package total leaves
    # none of it for them. The shared strings and styles are the parts they name.
    rels = pkg.rels(_WORKBOOK, _RELS_BYTES)
    named_parts: dict[str, str] = {}
    for kind, target in rels.values():
        named_parts.setdefault(kind, target)
    sheets, book = _sheets(pkg, rels)
    named = len(book.sheets) + book.more  # chart sheets included
    shared = _shared(pkg, named_parts.get("sharedStrings", ""))
    formats, unstyled = _formats(pkg, named_parts.get("styles", ""))
    reader = _Reader(shared.items, formats, book.date1904)
    acc, budget = Blocks(), Budget()
    tables: list[Table] = []
    failed: list[Failure] = []
    total = kept = 0
    unread_sheets = book.more  # each sheet not read is one unit: its rows are unknown
    # Units name sheets by their position in the workbook, chart sheets counted.
    if book.more:
        reason = f"{counted(book.more, 'sheet')} past {MAX_SHEETS}"
        fail(budget, failed, f"sheets {MAX_SHEETS + 1}-", reason)
    if book.names_cut:
        reason = f"{counted(book.names_cut, 'sheet name')} cut to {MAX_SECTION_CHARS} characters"
        fail(budget, failed, "sheet names", reason)
    if book.cut:  # the sheets the workbook names after the cut: one unit, their count unknown
        fail(budget, failed, f"sheets {named + 1}-", book.cut)
        unread_sheets += 1
    if shared.long:
        reason = f"{counted(shared.long, 'value')} over {MAX_VALUE_CHARS} characters, read empty"
        fail(budget, failed, "shared strings", reason)
    for i, (pos, name, part, hidden, kind) in enumerate(sheets):
        if budget.spent:  # nothing more fits: this sheet and those after it are not read
            left = len(sheets) - i
            # Cut by the package's bounds, or past the artifact's budget.
            reason = reader.exhausted or f"{counted(left, 'sheet')} past what one artifact holds"
            fail(budget, failed, f"sheets {pos}-", reason)
            unread_sheets += left
            break
        if not part:
            reason = f"{kind} sheets are not read" if kind else "its relationship names no part"
            fail(budget, failed, f"sheet {name}", reason)
            unread_sheets += 1
            continue
        # The merged ranges it read stay counted: their time is spent either way.
        before = budget.cells, budget.held, budget.cut, reader.uncalculated, reader.unshared
        try:
            rows, sheet_failed, unread = reader.rows(pkg, name, part, budget)
        except PartError as e:
            # Its rows, and what they counted, are not kept.
            budget.cells, budget.held, budget.cut, reader.uncalculated, reader.unshared = before
            fail(budget, failed, f"sheet {name}", str(e))
            unread_sheets += 1
            continue
        for f in sheet_failed:
            fail(budget, failed, f.unit, f.reason)
        total += unread
        if rows is None:
            continue  # no rows held: an empty sheet, or one whose failure is recorded above
        if not heading(budget, name):
            fail(budget, failed, f"sheet {name}", "past what one artifact holds")
            total += len(rows.rows) + rows.more  # its rows, none of them kept
            continue
        mark = len(acc.blocks)
        acc.heading(1, name, Locator(sheet=name), **({"hidden": True} if hidden else {}))
        t, table_failed = table(acc, f"t{i}", name, rows, budget, sheet=name)
        if t is not None:
            tables.append(replace(t, attrs={"hidden": True}) if hidden else t)
            kept += len(t.rows)
        else:  # no table: no heading over nothing, which would be a chunk of its own
            del acc.blocks[mark:]
        failed += table_failed
        total += len(rows.rows) + rows.more
    return Artifact(
        format="xlsx",
        parser="loom-xlsx",
        parser_version=on_python(PARSER_VERSION),  # `datetime` reads `t="d"` values
        blocks=tuple(acc.blocks),
        coverage=Coverage(
            "rows",
            total + unread_sheets,
            kept,
            failed=tuple(failed),
            lost=tuple(
                loss
                for loss, lost in (
                    ("formula_values", reader.uncalculated),
                    ("shared_strings", shared.cut or reader.unshared),
                    ("number_formats", unstyled),
                )
                if lost
            ),
        ),
        attrs={"sheets": named},
        tables=tuple(tables),
    )
