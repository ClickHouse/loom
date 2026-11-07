"""A PDF as blocks, read with pypdf: each page as it is laid out
(`pdflayout`), cited by its page. Furniture is left out and named; columns read
in order; ruled and borderless tables are typed tables whose rows are blocks
cited by pages and rows; headings come from the outline (its bookmarks) when
the file has one, else from the type. A page whose laid-out reading holds less
of its text than its text layer, or that is past the reader's bounds, is read
as its text layer: its text split at blank lines, the outline's headings at the
lines that hold their titles.

A page with no text is read with OCR (`ocr`) from the images it draws, and a
picture covering a fifth of a page with text is read into lines of its own;
where OCR is off, cannot read an image, or the image is drawn inline, the page
is partial or a failed unit naming why, so a page of images is never reported
complete when its text was not read. A page of text with a smaller picture,
which OCR leaves, is listed in `attrs.images_unread`, with one warning for the
file.

Pure and I/O-free.
"""

from __future__ import annotations

import bisect
import io
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

import pypdf
from pypdf.generic import ContentStream

from loom_extractor import ocr
from loom_extractor import pdflayout as layout
from loom_extractor.artifact import (
    MAX_SECTION_DEPTH,
    Artifact,
    Blocks,
    BlockType,
    Coverage,
    Failure,
    Locator,
    Table,
)
from loom_extractor.csvfile import typed_cell
from loom_extractor.tabular import EMPTY, Budget, Cell, Rows, counted
from loom_extractor.tabular import table as tabular_table

PARSER_VERSION = "4"  # a page of text names the pictures OCR leaves as not read
# A page's layout is trusted when its elements hold this share of the letters
# and digits its text layer holds (counted apart from spacing, which the text
# layer gets wrong where it runs two words together); under it the page is
# read as its text layer.
_TRUSTED = 0.998  # not a setting: a guard against a layout reading that drops text
_MAX_FURNITURE = 200  # not a setting: the furniture lines the artifact names
_NEEDS_OCR = "no text layer: its images need OCR, which is not run"


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


def _draws_inline_image(contents: Any, reader: pypdf.PdfReader) -> bool:
    """Whether a content stream draws an image inline (`BI ... ID ... EI`, as some
    scanners write a page): its operators are read, no image is decoded."""
    return any(op == b"INLINE IMAGE" for _, op in ContentStream(contents, reader).operations)


def _has_image(
    holder: Any,
    reader: pypdf.PdfReader,
    depth: int = 0,
    seen: set[tuple[str, int]] | None = None,
    inline_only: bool = False,
) -> bool:
    """Whether a page, or a form XObject on it, draws an image, as an image XObject
    or inline (with `inline_only`, inline alone); each form is searched once,
    however many times it is named."""
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
        if not hasattr(x, "get"):  # an entry that names no object: it may have been an image
            if inline_only:
                continue
            return True
        if x.get("/Subtype") == "/Image" and not inline_only:
            return True
        # An indirect object by its number; a direct one (no number) is its own.
        idnum = getattr(ref, "idnum", None)
        key = ("ref", idnum) if idnum is not None else ("obj", id(x))
        if x.get("/Subtype") != "/Form" or key in seen:
            continue
        if depth >= layout.MAX_FORM_DEPTH:  # a form nested too deep to search: it may draw one
            return True
        seen.add(key)
        if _has_image(x, reader, depth + 1, seen, inline_only):
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


def _page(
    acc: Blocks,
    text: str,
    number: int,
    headings: list[tuple[int, str]],
    shift: int = 0,
    **attrs: Any,
) -> None:
    """Page `number`'s blocks: its text split at the lines that hold a heading's
    title. Each heading, in outline order and `shift` levels down, takes the
    first line after the last heading's that holds its title; one that finds
    none follows the last heading (the page's start, before any)."""
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
        acc.heading(depth + shift, title, loc)
    run: list[str] = []
    for i, line in enumerate(lines):
        if i in at:
            for para in _paragraphs(run):
                acc.add("paragraph", para, loc, **attrs)
            run = []
            for depth, title in at[i]:
                acc.heading(depth + shift, title, loc)
        else:
            run.append(line)
    for para in _paragraphs(run):
        acc.add("paragraph", para, loc, **attrs)


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
    dropped: list[layout.Span] = field(default_factory=list)  # runs drawn twice, read once
    failure: str = ""  # the page is not read
    partial: str = ""  # the page is read, but not all of it: what was not
    ocr: bool = False  # some of its text was read from its images
    unread: bool = False  # a page of text with a picture OCR leaves: under a fifth of it
    # OCR's lines in a picture on a page with text (a diagram's labels): read
    # as text, apart from the page's tables and headings
    pictured: list[layout.Line] = field(default_factory=list)


def _read_layout(
    page: pypdf.PageObject, reader: pypdf.PdfReader, read: _Page, images: ocr.Images
) -> list[layout._Rule]:
    """The page's lines and rules, with what OCR reads in its pictures: on a
    page with no text, all of them, laid out as the page; on one with text,
    those that cover a fifth of it and that its own text does not already fill
    (a scan's text layer over its image), as lines of their own (`pictured`).
    An image drawn more than once on the page is read once, and a page reads
    at most `layout.MAX_PICTURES` images. What OCR could not read is the page's
    partial or failure."""
    found, read.other, read.width, read.page_height, turn = layout.spans(page, read.dropped)
    try:
        drew: layout.Drawing | None = layout.drawing(page, reader, turn)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 — unreadable drawing costs the ruled tables and pictures, not the text
        drew = None
    rules, pictures = (drew.rules, drew.pictures) if drew else ([], [])
    texted = bool(read.text.strip())
    area = read.width * read.page_height
    problems = []
    # each image once (`drawing` collects it where it is first drawn)
    targets = [
        p
        for p in pictures
        # one shown under a point wide (a spacer, a tracking pixel) holds no text
        if math.hypot(p.ctm[0], p.ctm[1]) >= 1
        and (not texted or (p.area >= ocr.PICTURE_SHARE * area and not _filled(found, p)))
    ]
    # The pictures OCR leaves on a page of text: under a fifth of it, and not a scan
    # its own text fills (a page partial for its pictures is named there instead).
    shown_inline = [p for p in (drew.inline if drew else []) if math.hypot(p.ctm[0], p.ctm[1]) >= 1]
    shown = [p for p in pictures if math.hypot(p.ctm[0], p.ctm[1]) >= 1]
    read.unread = texted and any(
        p.area < ocr.PICTURE_SHARE * area and not _filled(found, p) for p in [*shown, *shown_inline]
    )
    if len(targets) > layout.MAX_PICTURES:
        problems.append(f"it shows more than {layout.MAX_PICTURES} pictures OCR would read")
        targets = targets[: layout.MAX_PICTURES]
    # The images the page draws inline, there or in a form, which OCR does not
    # read, those under a point wide aside: `drawing` lists them, and only when it
    # did not read the page whole is the stream parsed again for one.
    draws_inline = not texted and (
        bool(shown_inline)
        if drew is not None and drew.whole
        else _has_image(page, reader, inline_only=True)
    )
    # A drawing read whole has seen every image the page draws, and one cut short is
    # reported below when the page may hold an image: the resources are searched here
    # only when there is no drawing.
    unseen = drew is None and _has_image(page, reader)
    # a page of only sub-point pictures has nothing to fail, unless it draws an image inline
    if not texted and not targets and (draws_inline or unseen):
        read.failure = (
            _NEEDS_OCR
            if ocr.engine() is None
            else "no text layer, and its drawing could not be read"
            if drew is None
            else "no text layer: its image is drawn inline, which OCR does not read"
        )
    for picture in targets:
        ref = getattr(picture.image, "indirect_reference", None)
        if ref is None:  # an image is a stream, which a file holds only as an indirect object
            problems.append("its image is not an object of the file")
            continue
        try:
            words, boxes, w, h = images.read(
                (ref.idnum, ref.generation),
                partial(_image_file, picture.image),
                # the inches of the image's own width, however it is turned
                math.hypot(picture.ctm[0], picture.ctm[1]) / 72,
            )
        except ValueError as e:
            problems.append(str(e))
            continue
        spans = layout.picture_spans(page, turn, picture, words, w, h)
        if texted:
            read.pictured += layout.lines_of(spans)
        else:  # a scanned page: laid out as a page is, its grid its rules
            found += spans
            rules += layout.picture_rules(page, turn, picture, boxes, w, h)
        read.ocr = True
    # after OCR's own reasons: one of them (OCR off) is what an operator can act on
    # an inline picture a fifth of a text page (its own text not filling it), or
    # any beside the pictures OCR read on a page with no text of its own
    if texted and any(
        p.area >= ocr.PICTURE_SHARE * area and not _filled(found, p) for p in shown_inline
    ):
        problems.append("a picture drawn inline covers a fifth of it, which OCR does not read")
    elif not texted and targets and draws_inline and drew is not None and drew.whole:
        problems.append("it draws a picture inline beside those OCR read, which OCR does not read")
    if drew is None and texted and _has_image(page, reader):
        problems.append("its drawing could not be read, so a picture on it may be unread")
    if drew is not None and not drew.whole and (pictures or _has_image(page, reader)):
        problems.append("not every picture it draws could be collected, so one may be unread")
    if problems:
        if texted:
            read.partial = (
                ocr.NOT_RUN
                if problems[0] == ocr.NOT_RUN
                else f"OCR did not read all of its pictures: {problems[0]}"
            )
        elif not read.ocr:
            read.failure = (
                _NEEDS_OCR
                if problems[0] == ocr.NOT_RUN
                else f"no text layer, and OCR read none of its images: {problems[0]}"
            )
        else:
            read.partial = f"OCR did not read all of its pictures: {problems[0]}"
        read.unread = False
    read.lines = layout.lines_of(found, layout.text_words(read.text), read.dropped)
    return rules


def _filled(found: list[layout.Span], picture: layout.Picture) -> bool:
    """Whether the page's own text runs fill a tenth or more of the picture: a
    scan's text layer drawn over its image, which OCR would read again."""
    x0, y0, x1, y1 = picture.box
    inked = sum(
        (s.x1 - s.x0) * s.size for s in found if x0 <= (s.x0 + s.x1) / 2 <= x1 and y0 <= s.y <= y1
    )
    return inked >= 0.1 * picture.area


def _image_file(image: Any) -> bytes:
    """An image XObject as a file Leptonica reads: a JPEG, JPEG 2000 or CCITT
    one as it is (pypdf wraps CCITT data as a TIFF), raw samples as a PNM (a
    CMYK one by its black). ValueError for what is not read (JBIG2, which would
    need a program run, a colour space past gray, RGB, CMYK and an index of
    gray or RGB)."""
    filters = image.get("/Filter")
    names = [
        str(f) for f in (filters if isinstance(filters, list) else [filters] if filters else [])
    ]
    if "/JBIG2Decode" in names:
        raise ValueError("its image is JBIG2, which OCR does not read")
    width, height = int(image.get("/Width", 0)), int(image.get("/Height", 0))
    if width <= 0 or height <= 0 or width * height > ocr.MAX_PIXELS:
        raise ValueError(f"its image is past {ocr.MAX_PIXELS:,} pixels or has no size")
    data = image.get_data()
    if names and names[-1] in ("/DCTDecode", "/JPXDecode", "/CCITTFaxDecode"):
        return bytes(data)
    if image.get("/ImageMask"):
        return ocr.pnm(width, height, 1, 1, data, black_is_one=_inverted(image))
    bits = int(image.get("/BitsPerComponent", 8))
    space, components, palette = _color_space(image.get("/ColorSpace"))
    if palette is not None:
        if bits != 8:
            raise ValueError(f"its image's {bits}-bit index is not read by OCR")
        index = data[: width * height]
        out = bytearray(len(index) * components)
        for c in range(components):  # each channel through its own table, not per pixel
            table = bytes(
                palette[i * components + c] if i * components + c < len(palette) else 255
                for i in range(256)
            )
            out[c::components] = index.translate(table)
        data = bytes(out)
    if space == "/DeviceCMYK" and bits == 8:
        data = data[3 : width * height * 4 : 4].translate(ocr.INVERT)  # its black, as gray
        components = 1
    elif bits == 8 and _inverted(image):
        data = data.translate(ocr.INVERT)
    return ocr.pnm(width, height, components, bits, data, black_is_one=_inverted(image))


def _inverted(image: Any) -> bool:
    decode = image.get("/Decode")
    return bool(decode) and float(decode[0]) == 1.0


def _color_space(cs: Any) -> tuple[str, int, bytes | None]:
    """A colour space's name, its components per sample, and an index's
    palette (each entry its base's components)."""
    cs = cs.get_object() if hasattr(cs, "get_object") else cs
    if isinstance(cs, list) and cs:
        kind = str(cs[0])
        if kind == "/ICCBased":
            n = int(cs[1].get_object().get("/N", 3))
            return (
                ("/DeviceGray", 1, None)
                if n == 1
                else ("/DeviceRGB", 3, None)
                if n == 3
                else ("/DeviceCMYK", 4, None)
            )
        if kind == "/Indexed" and len(cs) >= 4:
            base, components, _ = _color_space(cs[1])
            if components not in (1, 3):
                raise ValueError("its image's indexed colours are not read by OCR")
            lookup = cs[3].get_object()
            if hasattr(lookup, "get_data"):  # a stream
                table = lookup.get_data()
            else:  # a string, hex or literal
                table = getattr(lookup, "original_bytes", None) or str(lookup).encode("latin-1")
            return base, components, table
        cs = cs[0]
    name = str(cs or "/DeviceGray")
    if name in ("/DeviceGray", "/CalGray"):
        return "/DeviceGray", 1, None
    if name in ("/DeviceRGB", "/CalRGB"):
        return "/DeviceRGB", 3, None
    if name == "/DeviceCMYK":
        return "/DeviceCMYK", 4, None
    raise ValueError(f"its image's colour space {name} is not read by OCR")


def _layout_chars(read: _Page) -> Counter[str]:
    """The letters and digits the page's elements, furniture and other runs hold,
    and the second copies of runs drawn twice (the text layer holds both)."""
    chars: Counter[str] = Counter()
    for e in read.elements or []:
        if e.table is not None:
            for row in e.table.rows:
                chars.update(layout.text_chars(" ".join(row)))
        else:
            chars.update(layout.text_chars(e.text))
    for t in read.furniture:
        chars.update(layout.text_chars(t))
    for s in [*read.other, *read.dropped]:
        chars.update(layout.text_chars(s.text))
    return chars


def _merge_tables(reads: list[_Page]) -> None:
    """A table that a page ends with and the next page starts with, in the same
    columns, is one table, carried on over as many pages as go on with it; the
    next page's repeat of its header row is dropped. A page read as its text
    layer, failed or blank ends it."""
    open_: layout.TableBox | None = None  # the table the page before ended with
    before = 0  # that page's number
    for r in reads:
        els = r.elements
        if not els:
            open_ = None
            continue
        first = els[0].table
        if (
            open_ is not None
            and before == r.number - 1
            and first is not None
            and len(open_.x) == len(first.x)
            and all(abs(p - q) <= 6 for p, q in zip(open_.x, first.x, strict=True))
        ):
            rows = first.rows[1:] if first.rows[0] == open_.rows[0] else first.rows
            open_.row_pages = open_.row_pages or [before] * len(open_.rows)
            open_.rows.extend(rows)
            open_.row_pages.extend([r.number] * len(rows))
            els.pop(0)
            if not els:  # the page is all the table: it goes on
                before = r.number
                continue
        last = els[-1].table if els else None
        open_, before = last, r.number


def _place_outline(read: _Page, shift: int) -> None:
    """The page's outline entries as headings, `shift` levels down (under a
    title set larger than any they name). Each entry, in outline order, takes
    the first line after the last one placed that holds its title (or a
    paragraph whose text is its title), its paragraph split around it; one that
    finds none is a heading of its own after the last one placed (the page's
    start, before any). Linear in the page's lines, as the text layer's is."""
    els = read.elements or []
    # each text: the slots that hold it, in order
    where: dict[str, list[int]] = defaultdict(list)
    ends: dict[int, int] = {}  # a paragraph's whole slot: its last line's
    slots = 0  # a slot per element (a paragraph whole) and per line, in reading order
    for e in els:
        if e.kind == "table" or e.title:
            slots += 1
            continue
        if len(e.lines) > 1:
            where[_key(e.text)].append(slots)
            ends[slots] = slots + len(e.lines)  # its last line's slot
            slots += 1
        for ln in e.lines:
            where[_key(ln.text)].append(slots)
            slots += 1
    hit: dict[int, int] = {}  # a slot an entry takes: its depth
    # entries after a slot (-1: before any)
    after: dict[int, list[tuple[int, str]]] = defaultdict(list)
    last = -1
    for depth, title in read.headings:
        found = where.get(_key(title), [])
        k = bisect.bisect_right(found, last)
        if k < len(found):
            hit[found[k]] = depth
            # a paragraph that is a title is passed whole: no later entry takes its lines
            last = ends.get(found[k], found[k])
        else:
            after[last].append((depth, title))

    def heads(slot: int) -> list[layout.Element]:
        return [layout.Element("text", title=t, level=d + shift) for d, t in after.get(slot, [])]

    out = heads(-1)
    slot = 0
    for e in els:
        if e.kind == "table" or e.title:
            out.append(e)
            out += heads(slot)
            slot += 1
            continue
        whole = None
        if len(e.lines) > 1:
            whole, slot = slot, slot + 1
        if whole is not None and whole in hit:  # the paragraph is the title
            e.level = hit[whole] + shift
            out.append(e)
            out += heads(whole)
            for _ in e.lines:
                out += heads(slot)
                slot += 1
            continue
        if whole is not None:
            out += heads(whole)
        run: list[layout.Line] = []
        for ln in e.lines:
            if slot in hit:
                if run:
                    out.append(layout.Element(e.kind, run))
                    run = []
                out.append(layout.Element("text", [ln], level=hit[slot] + shift))
            else:
                run.append(ln)
            if after.get(slot):
                if run:
                    out.append(layout.Element(e.kind, run))
                    run = []
                out += heads(slot)
            slot += 1
        if run:
            out.append(e if len(run) == len(e.lines) else layout.Element(e.kind, run))
    read.elements = out


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
        marks = {"ocr": True} if e.ocr else {}
        if e.table is not None:
            _emit_table(acc, e.table, read.number, budget, tables, failed)
        elif e.level:
            acc.heading(e.level, e.text, loc, **marks)
        elif e.kind == "code":
            acc.add("code", e.text, loc, **marks)
        else:
            kind: BlockType = "list" if e.kind == "item" else "paragraph"
            acc.add(kind, e.text, loc, **marks)
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
        cells = {i: c for i, c in enumerate(_shown_cell(v) for v in row) if c.kind != "empty"}
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


def _shown_cell(text: str) -> Cell:
    """A cell typed as a CSV field is when its value reads back as the page
    shows it, else as that text: `1.50` and the version `1.10` stay text, as
    printing them back as numbers would change what the page says."""
    cell = typed_cell(text)
    return (
        cell
        if cell.kind in ("empty", "string") or cell.text == text.strip()
        else Cell(text.strip(), "string")
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
    images = ocr.Images()
    for index, page in enumerate(pages):
        number = index + 1
        read = _Page(number, [(d, _unicode(t)[0]) for d, t in outline.get(index, [])])
        reads.append(read)
        page_rules.append([])
        try:
            text = page.extract_text() or ""
        except MemoryError:
            raise  # the extraction's limit, not this page's defect
        except Exception as e:  # noqa: BLE001 — a page pypdf cannot read fails, not the file
            read.failure = f"its text could not be read: {type(e).__name__}"
            continue
        read.text, repaired = _unicode(text)
        if repaired:
            warnings.append(
                f"page {number}: a character its font maps to a lone surrogate reads U+FFFD"
            )
        try:
            page_rules[-1] = _read_layout(page, reader, read, images)
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001 — a page whose layout cannot be read is read as its text layer
            read.lines, read.pictured, read.ocr, read.unread = [], [], False, False
            try:
                imaged = _has_image(page, reader)
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001 — what the page draws is unknown: it may be an image
                imaged = True
            if not read.text.strip() and imaged:
                read.failure = (
                    f"no text layer, and its layout could not be read: {type(e).__name__}"
                )
            elif imaged:  # its text is read; a picture it draws may not be
                read.partial = (
                    "its layout could not be read, so a picture on it may be unread:"
                    f" {type(e).__name__}"
                )
    shift = _lay_out(reads, page_rules, outline_given=bool(outline), warnings=warnings)
    acc = Blocks()
    budget = Budget()
    tables: list[Table] = []
    for read in reads:
        if read.partial:
            failed.append(Failure(f"page {read.number}", read.partial))
        if read.failure:
            failed.append(Failure(f"page {read.number}", read.failure))
            # its headings still open their sections
            _page(acc, "", read.number, read.headings, shift)
        elif read.elements is None and read.ocr and not read.text.strip():
            # a scan's layout not trusted: its OCR lines are its text
            text = "\n".join(ln.text for ln in read.lines)
            _page(acc, text, read.number, read.headings, shift, ocr=True)
        elif read.elements is None:
            _page(acc, read.text, read.number, read.headings, shift)
        else:
            _emit(acc, read, budget, tables, failed)
        loc = Locator(page_start=read.number, page_end=read.number)
        for ln in read.pictured:
            acc.add("paragraph", ln.text, loc, ocr=True)
    attrs: dict[str, Any] = {"pages": len(pages)}
    ocr_pages = [r.number for r in reads if r.ocr]
    if ocr_pages:
        attrs["ocr_pages"] = ocr_pages
    unread = [r.number for r in reads if r.unread]
    if unread:
        attrs["images_unread"] = unread
        warnings.append(
            f"images on {counted(len(unread), 'page')} of text are not read: text drawn only"
            " inside a small image is not extracted (attrs.images_unread)"
        )
    # Furniture by its shape (digits aside): a running footer is one entry,
    # its first text and the pages it is on.
    furniture: dict[str, tuple[str, set[int]]] = {}  # a shape: its first text and its pages
    for read in reads:
        for t in read.furniture:
            furniture.setdefault(layout.shape(t), (_unicode(t)[0], set()))[1].add(read.number)
    if furniture:
        kinds = [(t, len(on)) for t, on in furniture.values()]
        attrs["furniture"] = [{"text": t, "pages": n} for t, n in kinds[:_MAX_FURNITURE]]
        cut = (
            f"; the first {_MAX_FURNITURE} of {len(kinds)} kinds named"
            if len(kinds) > _MAX_FURNITURE
            else ""
        )
        warnings.append(
            f"page furniture left out of the text: {sum(len(r.furniture) for r in reads)} header,"
            f" footer or page-number lines (attrs.furniture{cut})"
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
        parser_version=f"{PARSER_VERSION}+pypdf{pypdf.__version__}"
        + (f"+tesseract{images.version}" if images.version else ""),
        blocks=tuple(acc.blocks),
        coverage=Coverage(
            "pages",
            len(pages),
            len(pages) - sum(1 for r in reads if r.failure or r.partial),
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
) -> int:
    """Each readable page's elements, from its lines: columns in order, tables
    found, furniture out (a candidate no table holds), headings marked. A page whose
    elements hold less of its letters and digits than its text layer does, past
    the reader's line bound, or whose layout fails, is read as its text layer.
    The outline's level shift (`_outline_shift`), 0 without one."""
    live = [r for r in reads if not r.failure and r.lines]
    marks = layout.furniture([(r.lines, r.page_height) for r in live])
    candidates: dict[int, set[int]] = {}  # per page: the runs of its furniture candidates
    for k, r in enumerate(live):
        candidates[r.number] = {id(s) for i in marks.get(k, set()) for s in r.lines[i].spans}
    body, spacing = layout.body_style(
        ln
        for r in live
        for ln in r.lines
        if not all(id(s) in candidates[r.number] for s in ln.spans)
    )
    for r, found in zip(reads, page_rules, strict=True):
        if r.failure or not r.lines:
            continue
        if len(r.lines) > layout.MAX_LINES:
            reason = f"it holds more than {layout.MAX_LINES:,} lines"
        else:
            try:
                _lay_out_page(r, found, candidates.get(r.number, set()), spacing)
                reason = ""
            except MemoryError:
                raise
            except Exception as failure:  # noqa: BLE001 — a page the reader fails on is read as its text layer
                reason = f"its layout could not be read: {type(failure).__name__}"
        if not reason:
            want = layout.text_chars(r.text)
            if want and sum((want & _layout_chars(r)).values()) < _TRUSTED * sum(want.values()):
                reason = "its layout held less of its text"
        if reason:
            r.elements, r.furniture = None, []
            warnings.append(f"page {r.number}: read as its text layer; {reason}")
    _merge_tables(reads)
    layout.heading_levels([e for r in reads for e in (r.elements or [])], body)
    if not outline_given:
        return 0
    # The outline names the headings and their depth; the type keeps only what
    # is set above them (a title).
    shift = _outline_shift(reads)
    for r in reads:
        for e in r.elements or []:
            if e.level > shift:
                e.level = 0
        if r.elements is not None:
            _place_outline(r, shift)
    return shift


def _lay_out_page(r: _Page, found: list[layout._Rule], candidates: set[int], spacing: Any) -> None:
    gx = layout.gutter(r.lines, r.width)
    lines = layout.split_columns(r.lines, gx) if gx is not None else r.lines
    # A furniture candidate (a running header or footer with a letter, a page
    # number; a row of numbers is none): in a ruled table's box it is a cell,
    # and at a borderless table's end a row only when it reads as one.
    maybe = {
        i for i, ln in enumerate(lines) if ln.spans and all(id(s) in candidates for s in ln.spans)
    }
    tables = layout.ruled_tables(lines, found)
    held = set().union(*(t.lines for t in tables)) if tables else set()
    tables += layout.borderless_tables(lines, held, r.width, maybe)
    held = set().union(*(t.lines for t in tables)) if tables else set()
    furniture = maybe - held
    r.furniture = [lines[i].text for i in sorted(furniture)]
    index: dict[int, int] = {}  # a line's place among the lines kept
    kept: list[layout.Line] = []
    for i, ln in enumerate(lines):
        if i not in furniture:
            index[i] = len(kept)
            kept.append(ln)
    for t in tables:
        t.lines = {index[i] for i in t.lines}
    r.elements = layout.elements(kept, tables, spacing)
