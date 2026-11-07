"""A CSV or TSV file as one table, read with the standard library's `csv` over
the text `source.decode` gives: the delimiter sniffed from the file's start
(a comma, semicolon, tab or pipe; a tab, and no quoting, for a `.tsv` name), a
field quoted with `"` and its quotes doubled, the first row the header. A value
reads as text, except one written as a plain number, an ISO date or an ISO date
and time. A field over MAX_FIELD_CHARS, a record over MAX_RECORD_CHARS (a row of
a million empty fields is a list of a million in memory), or quoting the reader
cannot close, fails the rows from there on.

The rows are blocks too, cited by rows (`tabular`).

Pure and I/O-free.
"""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Iterator
from datetime import date, datetime

from loom_extractor.artifact import MAX_COLUMNS, Artifact, Blocks, Coverage, Failure, on_python
from loom_extractor.tabular import EMPTY, Budget, Cell, Rows, counted, fail, table

PARSER_VERSION = "2"
# A field longer than this is not a value a table holds.
MAX_FIELD_CHARS = 1 << 20  # not a setting: a bound on hostile input
MAX_RECORD_CHARS = 8 << 20  # not a setting: a bound on hostile input
# A line past the record bound is matched only that far, so it is never copied whole.
_LINE = re.compile(rf"[^\r\n]{{0,{MAX_RECORD_CHARS + 1}}}(?:\r\n|\r|\n)?")
_SNIFF_CHARS = 64 * 1024
# The sniffer's quote guess runs regexes that backtrack quadratically on a sample with an
# opening quote it never closes (`,"a` repeated: 16 s on 64 KB), so it reads only this much.
_QUOTE_SNIFF_CHARS = 4 * 1024
_MAX_NUMBER_CHARS = 40
_DELIMITERS = ",;\t|"
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
    r"(?:Z|[+-][0-9]{2}:?[0-9]{2})?"
)


def typed_cell(text: str) -> Cell:
    text = text.strip()
    if not text:
        return EMPTY
    # A number short enough to be one (no 5,000-digit int) and finite (not 1e999,
    # which is no JSON number); an integer past signed 64 bits is text, its
    # digits kept (an id, not a quantity).
    if len(text) <= _MAX_NUMBER_CHARS and _NUMBER.fullmatch(text):
        if "." not in text and "e" not in text.lower():
            n = int(text)
            return Cell(n, "number") if -(2**63) <= n < 2**63 else Cell(text, "string")
        number = float(text)
        if math.isfinite(number):
            return Cell(number, "number")
    try:
        if _DATE.fullmatch(text):
            return Cell(date.fromisoformat(text).isoformat(), "date")
        if _DATETIME.fullmatch(text):
            return Cell(datetime.fromisoformat(text).isoformat(), "datetime")
    except ValueError:
        pass  # 2024-13-45: text
    return Cell(text, "string")


class _Lines:
    """The text's lines, as the reader asks for them, failing a record that
    runs past MAX_RECORD_CHARS (`held` is reset as each record ends)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.held = 0

    def __iter__(self) -> Iterator[str]:
        for m in _LINE.finditer(self._text):
            line = m.group()
            if not line:
                continue
            self.held += len(line)
            if self.held > MAX_RECORD_CHARS:
                raise csv.Error(f"a record over {MAX_RECORD_CHARS} characters")
            yield line


class _Tsv(csv.excel_tab):
    """Tab-separated values as the format defines them: no quoting, so a value
    that starts with a quote is that text."""

    quoting = csv.QUOTE_NONE


def _whole_lines(text: str, chars: int) -> str:
    """The first `chars` of `text`, cut back to a line end: a cut line counts too few
    delimiters."""
    if len(text) <= chars:
        return text
    sample = text[:chars]
    return sample[: max(sample.rfind("\n"), sample.rfind("\r")) + 1] or sample


class _Sniffer(csv.Sniffer):
    """The standard sniffer, its quote guess bounded to `_QUOTE_SNIFF_CHARS`; the
    delimiter-frequency guess it falls back to still reads the whole sample, in linear
    time."""

    def _guess_quote_and_delimiter(
        self, data: str, delimiters: str | None
    ) -> tuple[str, bool, str, bool]:
        # The stdlib method `sniff` calls first; typeshed does not declare it.
        return super()._guess_quote_and_delimiter(  # type: ignore[misc,no-any-return]
            _whole_lines(data, _QUOTE_SNIFF_CHARS), delimiters
        )


def _dialect(text: str, filename: str) -> type[csv.Dialect] | csv.Dialect:
    if filename.lower().endswith(".tsv"):
        return _Tsv
    sample = _whole_lines(text, _SNIFF_CHARS)
    try:
        return _Sniffer().sniff(sample, delimiters=_DELIMITERS)
    except csv.Error:
        return csv.excel  # one column, or nothing the sniffer can tell


def extract_csv(text: str, filename: str = "") -> Artifact:
    # Process-wide, and the same bound on every call: the child reads one file.
    csv.field_size_limit(MAX_FIELD_CHARS)
    name = filename or "rows"
    lines = _Lines(text)
    # Strict, and quoted as spreadsheets write it, whatever the sniffer guesses.
    reader = csv.reader(
        lines, _dialect(text, filename), strict=True, doublequote=True, quotechar='"'
    )
    budget = Budget()
    rows: Rows | None = None
    failed: list[Failure] = []
    number = 0
    wide = 0
    broken = False
    try:
        for record in reader:
            lines.held = 0
            number += 1
            if not any(v.strip() for v in record):  # a blank record is no row: nothing to read
                continue
            if rows is not None and rows.more:  # past the budget: counted, not read
                rows.count(number)
                continue
            # A value past the columns a table holds is left out, and counted.
            wide += sum(1 for v in record[MAX_COLUMNS:] if v.strip())
            typed = (typed_cell(v) for v in record[:MAX_COLUMNS])
            cells = {i: c for i, c in enumerate(typed) if c.kind != "empty"}
            if not cells:
                continue
            if rows is None:
                # The header as the file writes it: a name is no value to type.
                header = [Cell(v.strip(), "string") for v in record[: max(cells) + 1]]
                rows = Rows([c if c.text else EMPTY for c in header], header_row=number)
                budget.collect([c for c in rows.header if c.text])  # held, as a sheet's is
            else:
                rows.add(number, cells, budget)
    except csv.Error as e:
        broken = True
        fail(budget, failed, f"{name} rows {number + 1}-", f"not CSV: {e}")
    if wide:
        fail(
            budget, failed, f"{name} columns", f"{counted(wide, 'value')} past column {MAX_COLUMNS}"
        )
    acc = Blocks()
    tables = []
    kept = 0
    if rows is not None:
        t, table_failed = table(acc, "t0", name, rows, budget)
        if t is not None:
            tables.append(t)
            kept = len(t.rows)
        failed += table_failed
    total = len(rows.rows) + rows.more if rows is not None else 0
    return Artifact(
        format="csv",
        parser="loom-csv",
        parser_version=on_python(PARSER_VERSION),  # `csv` and its sniffer decide the rows
        blocks=tuple(acc.blocks),
        # The rows read and the rows past the budget, and the rest of a file that broke off.
        coverage=Coverage("rows", total + broken, kept, failed=tuple(failed)),
        tables=tuple(tables),
    )
