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
from collections.abc import Iterator
from typing import Any

import pypdf
from pypdf.generic import ContentStream

from loom_extractor.artifact import (
    MAX_SECTION_DEPTH,
    Artifact,
    Blocks,
    Coverage,
    Failure,
    Locator,
)

PARSER_VERSION = "1"


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
        if _has_image(x, depth + 1, seen):
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
    acc = Blocks()
    failed: list[Failure] = []
    pages = reader.pages
    for index, page in enumerate(pages):
        number = index + 1
        headings = [(d, _unicode(t)[0]) for d, t in outline.get(index, [])]
        try:
            text = page.extract_text() or ""
            image_only = not text.strip() and _has_image(page, reader)
        except MemoryError:
            raise  # the extraction's limit, not this page's defect
        except Exception as e:  # noqa: BLE001 — a page pypdf cannot read fails, not the file
            reason = f"its text could not be read: {type(e).__name__}"
            failed.append(Failure(f"page {number}", reason))
            _page(acc, "", number, headings)  # its headings still open their sections
            continue
        if image_only:
            failed.append(
                Failure(f"page {number}", "no text layer: its images need OCR, which is not run")
            )
            _page(acc, "", number, headings)
            continue
        text, repaired = _unicode(text)
        if repaired:
            warnings.append(
                f"page {number}: a character its font maps to a lone surrogate reads U+FFFD"
            )
        _page(acc, text, number, headings)
    try:
        title = reader.metadata.title if reader.metadata else None
    except MemoryError:
        raise  # the extraction's limit, not the metadata's defect
    except Exception:  # noqa: BLE001 — broken metadata costs the title, not the text
        title = None
    title = _unicode(str(title))[0] if title else None
    return Artifact(
        format="pdf",
        parser="loom-pdf",
        parser_version=f"{PARSER_VERSION}+pypdf{pypdf.__version__}",
        blocks=tuple(acc.blocks),
        coverage=Coverage("pages", len(pages), len(pages) - len(failed), failed=tuple(failed)),
        warnings=tuple(warnings),
        attrs={"pages": len(pages), **({"title": title} if title else {})},
    )
