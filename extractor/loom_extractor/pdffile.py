"""A PDF's text layer as blocks, read with pypdf: each page's text in reading
order, cited by its page. The document's outline (its bookmarks) gives the
headings: an entry is a heading block on the page it points to, at the line
that holds its title (after the heading before it when no line does), and the
text after it sits in its section.

A PDF has no paragraphs of its own: a page's text is split at blank lines, and
a page with none is one block. Tables are read as their text, row by row as
the page lays them out. A page with no text that draws an image (an image
object, one in a form, or one inline in its content) is a failed unit: its
text needs OCR, which this parser does not run, so a page of images and no
text is never reported as complete.

Pure and I/O-free.
"""

from __future__ import annotations

import bisect
import io
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import pypdf
from pypdf.generic import ContentStream

from loom_extractor import pdflayout as layout
from loom_extractor.artifact import (
    MAX_SECTION_DEPTH,
    Artifact,
    Blocks,
    Coverage,
    Failure,
    Locator,
    Table,
)
from loom_extractor.csvfile import typed_cell
from loom_extractor.tabular import EMPTY, Budget, Cell, Rows
from loom_extractor.tabular import table as tabular_table

PARSER_VERSION = "2"
# A page's layout is trusted when its elements hold this share of the letters
# and digits its text layer holds (counted apart from spacing, which the text
# layer gets wrong where it runs two words together); under it the page is
# read as its text layer.
_TRUSTED = 0.998  # not a setting: a guard against a layout reading that drops text
_MAX_FURNITURE = 200  # not a setting: the furniture lines the artifact names


class EncryptedPdf(Exception):
    """A PDF that opens only with a password."""


def _outline(reader: pypdf.PdfReader) -> dict[int, list[tuple[int, str]]]:
    """Each page's outline entries, `{page index: [(depth, title), ...]}` in
    outline order; an entry that names no page of this file is left out, and
    one nested past MAX_SECTION_DEPTH is not read."""
    out: dict[int, list[tuple[int, str]]] = {}
    # Depth first, without recursion: pypdf gives a nested list, a list after an
    # entry holding its children, read before the entry's next sibling.
    stack: list[tuple[Iterator[Any], int]] = [(iter(reader.outline), 1)]
    while stack:
        entries, depth = stack[-1]
        entry = next(entries, None)
        if entry is None:
            stack.pop()
        elif isinstance(entry, list):
            if depth < MAX_SECTION_DEPTH:
                stack.append((iter(entry), depth + 1))
        else:
            title = " ".join(str(entry.title or "").split())
            page = reader.get_destination_page_number(entry)
            if title and page is not None and page >= 0:
                out.setdefault(page, []).append((depth, title))
    return out


# How deep form XObjects are searched for an image (a scan is often one image
# inside a form).
_MAX_FORM_DEPTH = 4  # not a setting: a bound on hostile input


def _draws_inline_image(contents: Any, reader: pypdf.PdfReader) -> bool:
    """Whether a content stream draws an image inline (`BI ... ID ... EI`, as some
    scanners write a page): its operators are read, no image is decoded."""
    return any(op == b"INLINE IMAGE" for _, op in ContentStream(contents, reader).operations)


def _has_image(
    holder: Any, reader: pypdf.PdfReader, depth: int = 0, seen: set[tuple[str, int]] | None = None
) -> bool:
    """Whether a page, or a form XObject on it, draws an image, as an image XObject
    or inline; each form is searched once, however many times it is named."""
    seen = set() if seen is None else seen
    contents = holder.get_contents() if isinstance(holder, pypdf.PageObject) else holder
    if contents is not None and _draws_inline_image(contents, reader):
        return True
    resources = holder.get("/Resources")
    xobjects = resources.get_object().get("/XObject") if resources else None
    if not xobjects:
        return False
    for ref in xobjects.get_object().values():
        x = ref.get_object()
        if x.get("/Subtype") == "/Image":
            return True
        # An indirect object by its number; a direct one (no number) is its own.
        idnum = getattr(ref, "idnum", None)
        key = ("ref", idnum) if idnum is not None else ("obj", id(x))
        if x.get("/Subtype") != "/Form" or depth >= _MAX_FORM_DEPTH or key in seen:
            continue
        seen.add(key)
        if _has_image(x, reader, depth + 1, seen):
            return True
    return False


# A lone surrogate, which a font's broken character map can give and no UTF-8
# reader takes.
_SURROGATE = re.compile("[\ud800-\udfff]")


def _unicode(text: str) -> tuple[str, bool]:
    """`text` with each lone surrogate as U+FFFD, and whether there was one."""
    out, n = _SURROGATE.subn("\ufffd", text)
    return out, bool(n)


def _paragraphs(lines: list[str]) -> list[str]:
    out: list[str] = []
    run: list[str] = []
    for line in [*lines, ""]:
        if line.strip():
            run.append(line.rstrip())
        elif run:
            out.append("\n".join(run))
            run = []
    return out


def _page(acc: Blocks, text: str, number: int, headings: list[tuple[int, str]]) -> None:
    """Page `number`'s blocks: its text split at the lines that hold a heading's
    title. Each heading, in outline order, takes the first line after the last
    heading's that holds its title; one that finds none follows the last heading
    (the page's start, before any)."""
    loc = Locator(page_start=number, page_end=number)
    lines = text.split("\n")
    where: dict[str, list[int]] = {}  # each line's text: the lines that hold it, in order
    for i, line in enumerate(lines):
        where.setdefault(" ".join(line.split()), []).append(i)
    at: dict[int, list[tuple[int, str]]] = {}  # a title's line: the headings there
    first: list[tuple[int, str]] = []  # headings before the page's text
    last = -1  # the line the last placed heading took
    for depth, title in headings:
        found = where.get(title, [])
        k = bisect.bisect_right(found, last)
        if k < len(found):
            last = found[k]
        (at.setdefault(last, []) if last >= 0 else first).append((depth, title))
    for depth, title in first:
        acc.heading(depth, title, loc)
    run: list[str] = []
    for i, line in enumerate(lines):
        if i in at:
            for para in _paragraphs(run):
                acc.add("paragraph", para, loc)
            run = []
            for depth, title in at[i]:
                acc.heading(depth, title, loc)
        else:
            run.append(line)
    for para in _paragraphs(run):
        acc.add("paragraph", para, loc)


@dataclass
class _Page:
    """One page as read: its layout's elements, or its text layer alone when
    the layout cannot be trusted with it, or why it failed."""

    number: int
    headings: list[tuple[int, str]]
    text: str = ""
    elements: list[layout.Element] | None = None
    lines: list[layout.Line] = field(default_factory=list)
    width: float = 0.0
    page_height: float = 0.0
    other: list[layout.Span] = field(default_factory=list)
    furniture: list[str] = field(default_factory=list)
    failure: str = ""


def _read_layout(
    page: pypdf.PageObject, reader: pypdf.PdfReader, read: _Page
) -> list[layout._Rule]:
    found, read.other, read.width, read.page_height, turn = layout.spans(page)
    read.lines = layout.lines_of(found, layout.text_words(read.text))
    try:
        return layout.rules(page, reader, turn)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 — unreadable drawing costs the ruled tables, not the text
        return []


def _layout_chars(read: _Page) -> Counter[str]:
    """The letters and digits the page's elements, furniture and other runs hold."""
    chars: Counter[str] = Counter()
    for e in read.elements or []:
        if e.table is not None:
            for row in e.table.rows:
                chars.update(layout.text_chars(" ".join(row)))
        else:
            chars.update(layout.text_chars(e.text))
    for t in read.furniture:
        chars.update(layout.text_chars(t))
    for s in read.other:
        chars.update(layout.text_chars(s.text))
    return chars


def _merge_tables(pages: list[_Page]) -> None:
    """A table that a page ends with and the next page starts with, in the same
    columns, is one table; the next page's repeat of its header row is dropped."""
    for a, b in zip(pages, pages[1:], strict=False):
        if not a.elements or not b.elements:
            continue
        first, last = b.elements[0], a.elements[-1]
        if first.table is None or last.table is None:
            continue
        ta, tb = last.table, first.table
        if len(ta.x) != len(tb.x) or any(abs(p - q) > 6 for p, q in zip(ta.x, tb.x, strict=True)):
            continue
        rows = tb.rows[1:] if tb.rows[0] == ta.rows[0] else tb.rows
        ta.row_pages = ta.row_pages or [a.number] * len(ta.rows)
        ta.rows.extend(rows)
        ta.row_pages.extend([b.number] * len(rows))
        b.elements.pop(0)


def _place_outline(read: _Page, shift: int) -> None:
    """The page's outline entries as headings, each `shift` levels below the
    headings set larger than any the outline names (a title it leaves out): an
    entry takes the first line after the last one placed that holds its title,
    its element split around it, else is a heading of its own after it."""
    els = read.elements or []
    last = -1  # the element the last placed heading is
    for depth, title in read.headings:
        key = _key(title)
        hit: tuple[int, int | None] | None = None
        for k in range(last + 1, len(els)):
            e = els[k]
            if e.kind == "table" or e.title:
                continue
            if _key(e.text) == key:
                hit = (k, None)
                break
            at = next((i for i, ln in enumerate(e.lines) if _key(ln.text) == key), None)
            if at is not None:
                hit = (k, at)
                break
        if hit is None:
            els.insert(last + 1, layout.Element("text", title=title, level=depth + shift))
            last += 1
            continue
        k, at = hit
        if at is not None:  # the title is a line inside an element: it becomes one of its own
            e = els[k]
            parts = [
                layout.Element(e.kind, e.lines[:at]),
                layout.Element("text", [e.lines[at]]),
                layout.Element(e.kind, e.lines[at + 1 :]),
            ]
            kept = [p for p in parts if p.lines]
            els[k : k + 1] = kept
            k += 1 if parts[0].lines else 0
        els[k].level = depth + shift
        last = k


def _key(text: str) -> str:
    return " ".join(text.split())


def _outline_shift(reads: list[_Page]) -> int:
    """How many heading levels, by style, sit above the outline's first level:
    the style level of the elements its top entries title, less one."""
    top = {_key(t) for r in reads for d, t in r.headings if d == 1}
    levels = [e.level for r in reads for e in r.elements or [] if e.level and _key(e.text) in top]
    return max(0, min(levels) - 1) if levels else 0


def _emit(
    acc: Blocks, read: _Page, budget: Budget, tables: list[Table], failed: list[Failure]
) -> None:
    loc = Locator(page_start=read.number, page_end=read.number)
    for e in read.elements or []:
        if e.table is not None:
            _emit_table(acc, e.table, read.number, budget, tables, failed)
        elif e.level:
            acc.heading(e.level, e.text, loc)
        elif e.kind == "code":
            acc.add("code", e.text, loc)
        else:
            acc.add("list" if e.kind == "item" else "paragraph", e.text, loc)
    for s in read.other:
        acc.add("paragraph", s.text, loc, orientation="rotated")


def _emit_table(
    acc: Blocks,
    box: layout.TableBox,
    page: int,
    budget: Budget,
    tables: list[Table],
    failed: list[Failure],
) -> None:
    """A table as the tabular sources' are: its rows as blocks of `column: value`
    lines and a typed table, each cited by its pages and rows (the header is row 1)."""
    pages = box.row_pages or [page] * len(box.rows)
    tid = f"t{len(tables)}"
    name = f"page {page} table {sum(1 for t in tables if t.locator.page_start == page) + 1}"
    head = [Cell(h, "string") if h else EMPTY for h in box.rows[0]]
    rows = Rows(head, header_row=1)
    for n, row in enumerate(box.rows[1:], start=2):
        cells = {i: c for i, c in enumerate(typed_cell(v) for v in row) if c.kind != "empty"}
        if cells:
            rows.add(n, cells, budget)
    start = len(acc.blocks)
    table, table_failed = tabular_table(acc, tid, name, rows, budget)
    failed.extend(table_failed)
    for k in range(start, len(acc.blocks)):
        b = acc.blocks[k]
        lo, hi = pages[b.locator.row_start - 1], pages[b.locator.row_end - 1]
        acc.blocks[k] = replace(
            b,
            locator=Locator(
                page_start=lo, page_end=hi, row_start=b.locator.row_start, row_end=b.locator.row_end
            ),
        )
    if table is not None:
        last = table.locator.row_end
        tables.append(
            replace(
                table,
                locator=Locator(
                    page_start=pages[0], page_end=pages[last - 1], row_start=1, row_end=last
                ),
            )
        )


def extract_pdf(data: bytes) -> Artifact:
    reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise EncryptedPdf("an encrypted PDF; save it without a password")
    try:
        outline = _outline(reader)
    except MemoryError:
        raise  # the extraction's limit, not the outline's defect
    except Exception:  # noqa: BLE001 — a broken outline costs the headings, not the text
        outline = {}
    warnings: list[str] = []
    failed: list[Failure] = []
    pages = reader.pages
    reads: list[_Page] = []
    page_rules: list[list[layout._Rule]] = []
    for index, page in enumerate(pages):
        number = index + 1
        read = _Page(number, [(d, _unicode(t)[0]) for d, t in outline.get(index, [])])
        reads.append(read)
        page_rules.append([])
        try:
            text = page.extract_text() or ""
            image_only = not text.strip() and _has_image(page, reader)
        except MemoryError:
            raise  # the extraction's limit, not this page's defect
        except Exception as e:  # noqa: BLE001 — a page pypdf cannot read fails, not the file
            read.failure = f"its text could not be read: {type(e).__name__}"
            continue
        if image_only:
            read.failure = "no text layer: its images need OCR, which is not run"
            continue
        read.text, repaired = _unicode(text)
        if repaired:
            warnings.append(
                f"page {number}: a character its font maps to a lone surrogate reads U+FFFD"
            )
        try:
            page_rules[-1] = _read_layout(page, reader, read)
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 — a page whose layout cannot be read is read as its text layer
            read.lines = []
    _lay_out(reads, page_rules, outline_given=bool(outline), warnings=warnings)
    acc = Blocks()
    budget = Budget()
    tables: list[Table] = []
    for read in reads:
        if read.failure:
            failed.append(Failure(f"page {read.number}", read.failure))
            _page(acc, "", read.number, read.headings)  # its headings still open their sections
        elif read.elements is None:
            _page(acc, read.text, read.number, read.headings)
        else:
            _emit(acc, read, budget, tables, failed)
    furniture = Counter(_unicode(t)[0] for read in reads for t in read.furniture)
    attrs: dict[str, Any] = {"pages": len(pages)}
    if furniture:
        attrs["furniture"] = [
            {"text": t, "pages": n} for t, n in sorted(furniture.items())[:_MAX_FURNITURE]
        ]
        warnings.append(
            f"page furniture left out of the text: {sum(furniture.values())} header, footer"
            " or page-number lines (attrs.furniture)"
        )
    try:
        title = reader.metadata.title if reader.metadata else None
    except MemoryError:
        raise  # the extraction's limit, not the metadata's defect
    except Exception:  # noqa: BLE001 — broken metadata costs the title, not the text
        title = None
    title = _unicode(str(title))[0] if title else None
    if title:
        attrs["title"] = title
    return Artifact(
        format="pdf",
        parser="loom-pdf",
        parser_version=f"{PARSER_VERSION}+pypdf{pypdf.__version__}",
        blocks=tuple(acc.blocks),
        coverage=Coverage(
            "pages",
            len(pages),
            len(pages) - sum(1 for r in reads if r.failure),
            failed=tuple(failed),
        ),
        warnings=tuple(warnings),
        attrs=attrs,
        tables=tuple(tables),
    )


def _lay_out(
    reads: list[_Page],
    page_rules: list[list[layout._Rule]],
    *,
    outline_given: bool,
    warnings: list[str],
) -> None:
    """Each readable page's elements, from its lines: furniture out, columns in
    order, tables found, headings marked; a page whose elements hold fewer of
    its words than its text layer does is read as its text layer."""
    live = [r for r in reads if not r.failure and r.lines]
    marks = layout.furniture([(r.lines, r.page_height) for r in live])
    for k, r in enumerate(live):
        drop = marks.get(k, set())
        r.furniture = [ln.text for i, ln in enumerate(r.lines) if i in drop]
        r.lines = [ln for i, ln in enumerate(r.lines) if i not in drop]
    body, spacing = layout.body_style(ln for r in live for ln in r.lines)
    for r, found in zip(reads, page_rules, strict=True):
        if r.failure or not r.lines:
            continue
        gx = layout.gutter(r.lines, r.width)
        lines = layout.split_columns(r.lines, gx) if gx is not None else r.lines
        tables = layout.ruled_tables(lines, found)
        held = set().union(*(t.lines for t in tables)) if tables else set()
        tables += layout.borderless_tables(lines, held, r.width)
        r.elements = layout.elements(lines, tables, spacing)
        want = layout.text_chars(r.text)
        got = _layout_chars(r)
        if want and sum((want & got).values()) < _TRUSTED * sum(want.values()):
            r.elements = None
            warnings.append(
                f"page {r.number}: read as its text layer; its layout held less of its text"
            )
    _merge_tables([r for r in reads if r.elements is not None])
    layout.heading_levels([e for r in reads for e in (r.elements or [])], body)
    if outline_given:
        # The outline names the headings and their depth; the style keeps only
        # what is set above them (a title).
        shift = _outline_shift(reads)
        for r in reads:
            for e in r.elements or []:
                if e.level > shift:
                    e.level = 0
            if r.elements is not None:
                _place_outline(r, shift)
