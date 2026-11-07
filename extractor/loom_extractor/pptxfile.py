"""A PPTX deck as blocks, read from its XML parts with the standard library:
slide by slide in the deck's order, cited by slide number. A slide's title is
a heading its other blocks sit under; each other text shape is a block, in the
slide's order (grouped shapes in place), a table one block (a row per line,
its cells joined by ` | `, `attrs.table` its shape), and the speaker notes a
block marked `attrs.notes`. A hidden slide is read and marked `attrs.hidden`.

A shape PowerPoint wraps in alternate content (an equation, a newer shape
kind) is read through its first choice. A picture that covers a fifth of its
slide or more may carry text of its own (a screenshot, a whiteboard photo):
OCR reads it into a block marked `attrs.ocr` after the slide's shapes, and
without OCR the slide is a failed unit. A slide that holds pictures, charts,
diagrams or embedded objects and no text is a failed unit: what it shows is in
them, which this parser does not read. A slide of text with a picture OCR does
not read (one under a fifth of it, one as a fill or background, or an
alternate content's fallback picture), a chart, a diagram or an embedded object
is read for its text and listed in `attrs.images_unread`, with one warning for
the file, unless the slide failed for what it shows: its failure names the first
thing that stopped it, not all it holds.

Pure and I/O-free.
"""

from __future__ import annotations

from functools import partial
from xml.etree.ElementTree import Element

from loom_extractor import ocr
from loom_extractor.artifact import Artifact, Blocks, Coverage, Failure, Locator
from loom_extractor.ooxml import Package, PartError, q
from loom_extractor.tabular import counted

PARSER_VERSION = "4"  # a slide of text names what it does not read
_EMU_PER_INCH = 914_400
_DEFAULT_SLIDE = (9_144_000, 6_858_000)  # a deck that names no slide size: 10 x 7.5 inches
_MAX_PICTURE_BYTES = 32 * 1024 * 1024  # not a setting: an image part OCR reads
_PRESENTATION = "ppt/presentation.xml"
_TITLES = {"title", "ctrTitle"}
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_A14_MATH = "{http://schemas.microsoft.com/office/drawing/2010/main}m"  # an equation
# The kinds of shape the walk reads.
_SHAPES = {q("p:sp"), q("p:pic"), q("p:grpSp"), q("p:graphicFrame")}


def _paragraphs(body: Element) -> str:
    lines = []
    for p in body.iter(q("a:p")):
        parts = []
        for el in p.iter():
            if el.tag == q("a:t"):
                parts.append(el.text or "")
            elif el.tag == q("a:br"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(line for line in lines if line.strip()).strip()


def _table(tbl: Element) -> tuple[str, int, int]:
    rows = [
        [" ".join(_paragraphs(tc).split()) for tc in tr.findall(q("a:tc"))]
        for tr in tbl.findall(q("a:tr"))
    ]
    columns = max((len(r) for r in rows), default=0)
    return "\n".join(" | ".join(r) for r in rows), len(rows), columns


def _placeholder(sp: Element) -> str:
    ph = sp.find(f"{q('p:nvSpPr')}/{q('p:nvPr')}/{q('p:ph')}")
    return "" if ph is None else ph.get("type", "body")


def _size(ext: Element | None) -> tuple[int, int] | None:
    """An extent's `cx` and `cy` in EMU; None when it is missing or not positive."""
    try:
        cx, cy = (int(ext.get("cx", "0")), int(ext.get("cy", "0"))) if ext is not None else (0, 0)
    except ValueError:
        return None
    return (cx, cy) if cx > 0 and cy > 0 else None


def _extent(el: Element, xfrm: str = "p:spPr", ext: str = "a:ext") -> tuple[int, int] | None:
    """A shape's width and height in EMU, from its own transform; None when it has none."""
    return _size(el.find(f"{q(xfrm)}/{q('a:xfrm')}/{q(ext)}"))


class _Slide:
    """One slide's title, its text shapes and tables in order, and whether it
    holds what is not read (a picture, a chart, a diagram, an embedded object)."""

    def __init__(self, root: Element) -> None:
        self.title = ""
        self.shapes: list[tuple[str, dict[str, object]]] = []  # (text, attrs)
        self.charts = False  # a chart, diagram or embedded object among them
        # A picture OCR never sees: the background here, a fill or a fallback in the walk.
        self.unread_picture = (
            root.find(f"{q('p:cSld')}/{q('p:bg')}/{q('p:bgPr')}/{q('a:blipFill')}") is not None
        )
        self.picture_count = 0  # its pictures, those `pictures` lists and any other
        # (relationship id, its width and height in EMU, or the placeholder it takes
        # its size from when it states none)
        self.pictures: list[tuple[str, tuple[int, int] | Element]] = []
        tree = root.find(f"{q('p:cSld')}/{q('p:spTree')}")
        if tree is not None:
            self._walk(tree, 1.0, 1.0)

    def _walk(self, tree: Element, sx: float, sy: float) -> None:
        """`sx` and `sy` scale the extents of shapes in `tree` to the slide's: a
        group's children are sized in its own child space."""
        for el in tree:
            if el.tag == q("p:grpSp"):
                if el.find(f"{q('p:grpSpPr')}/{q('a:blipFill')}") is not None:
                    self.unread_picture = True  # a group filled with a picture
                ext, child = _extent(el, "p:grpSpPr"), _extent(el, "p:grpSpPr", "a:chExt")
                if ext and child:
                    self._walk(el, sx * ext[0] / child[0], sy * ext[1] / child[1])
                else:
                    self._walk(el, sx, sy)
            elif el.tag == f"{_MC}AlternateContent":
                choice = el.find(f"{_MC}Choice")
                if choice is not None:
                    self._walk(choice, sx, sy)
                # A choice of a kind the walk does not know (ink), or an equation, whose
                # math is no text the walk reads, shows its picture in the fallback.
                fallback = el.find(f"{_MC}Fallback")
                known = (
                    choice is not None
                    and any(c.tag in _SHAPES for c in choice)
                    and choice.find(f".//{_A14_MATH}") is None
                )
                if (
                    fallback is not None
                    and not known
                    and (
                        fallback.find(f".//{q('p:pic')}") is not None
                        or fallback.find(f".//{q('a:blipFill')}") is not None
                    )
                ):
                    self.unread_picture = True
            elif el.tag == q("p:pic"):
                self.picture_count += 1
                blip = el.find(f"{q('p:blipFill')}/{q('a:blip')}")
                if blip is None:
                    continue
                rid = blip.get(q("r:embed"), "")  # empty for a linked picture
                size = _extent(el)
                ph = el.find(f"{q('p:nvPicPr')}/{q('p:nvPr')}/{q('p:ph')}")
                if size is not None:
                    self.pictures.append((rid, (round(size[0] * sx), round(size[1] * sy))))
                elif ph is not None:
                    self.pictures.append((rid, ph))
            elif el.tag == q("p:sp"):
                if el.find(f"{q('p:spPr')}/{q('a:blipFill')}") is not None:
                    self.unread_picture = True  # a shape filled with a picture
                body = el.find(q("p:txBody"))
                text = _paragraphs(body) if body is not None else ""
                if not text:
                    continue
                if _placeholder(el) in _TITLES and not self.title:
                    self.title = " ".join(text.split())
                else:
                    self.shapes.append((text, {}))
            elif el.tag == q("p:graphicFrame"):
                tables = list(el.iter(q("a:tbl")))
                if not tables:
                    self.charts = True  # a chart, a diagram or an embedded object
                elif el.find(f".//{q('a:tcPr')}/{q('a:blipFill')}") is not None:
                    self.unread_picture = True  # a table cell filled with a picture
                for tbl in tables:
                    text, rows, columns = _table(tbl)
                    if text.strip(" |\n"):
                        self.shapes.append((text, {"table": {"rows": rows, "columns": columns}}))


def _notes(pkg: Package, rels: dict[str, tuple[str, str]]) -> str:
    """The speaker notes of the slide whose relationships are `rels`."""
    for kind, target in rels.values():
        if kind == "notesSlide":
            tree = pkg.xml(target).find(f"{q('p:cSld')}/{q('p:spTree')}")
            if tree is None:
                return ""
            # The notes are the body placeholder; the others hold the slide's image
            # and its number.
            return "\n".join(
                _paragraphs(body)
                for sp in tree.iter(q("p:sp"))
                if _placeholder(sp) == "body" and (body := sp.find(q("p:txBody"))) is not None
            ).strip()
    return ""


def _slide_parts(pkg: Package) -> list[str | None]:
    """The deck's slide parts in its order; None for a slide id whose relationship
    names no slide part, which is still a slide of the deck."""
    rels = pkg.rels(_PRESENTATION)
    ids = pkg.xml(_PRESENTATION).find(q("p:sldIdLst"))
    parts: list[str | None] = []
    for sld in ids if ids is not None else []:
        kind, target = rels.get(sld.get(q("r:id"), ""), ("", ""))
        parts.append(target if kind == "slide" else None)
    return parts


def _slide_size(pkg: Package) -> tuple[int, int]:
    return _size(pkg.xml(_PRESENTATION).find(q("p:sldSz"))) or _DEFAULT_SLIDE


# A layout's or master's placeholders: (idx, type, size or None) each.
_Entries = list[tuple[str | None, str, tuple[int, int] | None]]
# The master placeholder each type inherits from: a content placeholder (a picture,
# table, chart or one of no type) takes the body's.
_MASTER_TYPE = {"title": "title", "ctrTitle": "title", "dt": "dt", "ftr": "ftr", "sldNum": "sldNum"}


def _entries(pkg: Package, part: str) -> _Entries:
    tree = pkg.xml(part).find(f"{q('p:cSld')}/{q('p:spTree')}")
    return [
        (ph.get("idx"), ph.get("type", "obj"), _extent(el))
        for el in (tree.iter() if tree is not None else [])
        if (ph := el.find(f"*/{q('p:nvPr')}/{q('p:ph')}")) is not None
    ]


class _Holders:
    """The deck's layouts' and masters' placeholders, each part and each layout's
    relationships read once a deck, however many pictures sit in them: one that
    cannot be read fails every slide that asks for it, without a second read."""

    def __init__(self, pkg: Package) -> None:
        self._pkg = pkg
        self._parts: dict[str, _Entries | PartError | RecursionError] = {}
        self._masters: dict[str, str | None | PartError | RecursionError] = {}

    def _part(self, part: str) -> _Entries:
        if part not in self._parts:
            try:
                self._parts[part] = _entries(self._pkg, part)
            except (PartError, RecursionError) as e:
                self._parts[part] = e
        got = self._parts[part]
        if isinstance(got, Exception):
            raise got
        return got

    def placeholders(self, layout: str) -> tuple[_Entries, _Entries]:
        """The layout's placeholders and its master's."""
        if layout not in self._masters:
            try:
                rels = self._pkg.rels(layout).values()
                self._masters[layout] = next((t for k, t in rels if k == "slideMaster"), None)
            except (PartError, RecursionError) as e:
                self._masters[layout] = e
        master = self._masters[layout]
        if isinstance(master, Exception):
            raise master
        return self._part(layout), self._part(master) if master else []


def _placeholder_extent(ph: Element, layout: _Entries, master: _Entries) -> tuple[int, int] | None:
    """The size of the placeholder `ph` names: its layout's placeholder of the same
    index, else of the same type, when that one is sized; else the master's that
    the layout's (or its own) type inherits from."""
    idx, kind = ph.get("idx"), ph.get("type", "obj")
    entry = next((e for e in layout if idx is not None and e[0] == idx), None) or next(
        (e for e in layout if e[1] == kind), None
    )
    if entry is not None:
        if entry[2] is not None:
            return entry[2]
        kind = entry[1]
    want = _MASTER_TYPE.get(kind, "body")
    return next((e[2] for e in master if e[1] == want and e[2] is not None), None)


def _picture_texts(
    pkg: Package,
    rels: dict[str, tuple[str, str]],
    slide: _Slide,
    size: tuple[int, int],
    images: ocr.Images,
    holders: _Holders,
) -> tuple[list[str], int, str]:
    """The text OCR reads in each of the slide's pictures that covers a fifth of
    it, how many it read, and why the first it could not read was not (empty
    when it read all). An image several slides show is read once."""
    if not slide.pictures:
        return [], 0, ""
    width, height = size
    layout = next((t for k, t in rels.values() if k == "slideLayout"), None)
    placed = any(isinstance(e, Element) for _, e in slide.pictures)
    held = holders.placeholders(layout) if layout is not None and placed else ([], [])
    # a placeholder whose size is nowhere stated is taken as the slide's
    sized = [
        (rid, e if isinstance(e, tuple) else _placeholder_extent(e, *held) or size)
        for rid, e in slide.pictures
    ]
    big = [(rid, cx) for rid, (cx, cy) in sized if cx * cy >= ocr.PICTURE_SHARE * width * height]
    if not big:
        return [], 0, ""
    texts: list[str] = []
    read = 0
    why = ""
    for rid, cx in big:
        kind, target = rels.get(rid, ("", "")) if rid else ("", "")
        if kind != "image":  # linked, or its relationship names no image in the file
            why = why or "a picture's image is not held in the file, so OCR cannot read it"
            continue
        try:
            words, _, _, _ = images.read(
                target, partial(pkg.media, target, _MAX_PICTURE_BYTES), cx / _EMU_PER_INCH
            )
        except ValueError as e:
            why = why or (
                str(e) if str(e) == ocr.NOT_RUN else f"a picture could not be read with OCR: {e}"
            )
            continue
        read += 1
        lines: list[list[str]] = []
        for w in words:
            if w.first or not lines:
                lines.append([])
            lines[-1].append(w.text)
        text = "\n".join(" ".join(line) for line in lines).strip()
        if text:
            texts.append(text)
    return texts, read, why


def extract_pptx(data: bytes) -> Artifact:
    pkg = Package(data)
    parts = _slide_parts(pkg)
    size = _slide_size(pkg)
    images = ocr.Images()
    holders = _Holders(pkg)
    ocr_slides: list[int] = []
    not_read: list[int] = []  # slides of text showing what is not read
    acc = Blocks()
    failed: list[Failure] = []
    incomplete: set[int] = set()  # slides with a failure, their own or their notes'
    for number, part in enumerate(parts, 1):
        loc = Locator(page_start=number, page_end=number)
        if part is None:
            failed.append(Failure(f"slide {number}", "its relationship names no slide part"))
            incomplete.add(number)
            continue
        try:
            root = pkg.xml(part)
            slide = _Slide(root)
        except (PartError, RecursionError) as e:  # nested past what the walk reads too
            failed.append(Failure(f"slide {number}", str(e) or type(e).__name__))
            incomplete.add(number)
            continue
        # `show` is an XML boolean.
        hidden = {"hidden": True} if root.get("show") in ("0", "false") else {}
        notes = ""
        pictured: list[str] = []
        try:
            rels = pkg.rels(part)
        except (PartError, RecursionError) as e:  # the slide's text is still read
            seen, unread = 0, f"its relationships could not be read: {e}"
        else:
            try:
                notes = _notes(pkg, rels)
            except (PartError, RecursionError) as e:  # the slide is still read; its notes are not
                failed.append(Failure(f"slide {number} notes", str(e)))
                incomplete.add(number)
            # a layout or master that cannot be read: the slide's pictures are not read
            try:
                pictured, seen, unread = _picture_texts(pkg, rels, slide, size, images, holders)
            except (PartError, RecursionError) as e:
                pictured, seen, unread = [], 0, f"its pictures could not be read: {e}"
        if unread:
            failed.append(Failure(f"slide {number}", unread))
            incomplete.add(number)
        if seen:
            ocr_slides.append(number)
        # pictures that OCR read every one of, and found no text in, lose nothing
        shows_unread = slide.charts or slide.unread_picture or seen < slide.picture_count
        if not (slide.title or slide.shapes or notes or pictured):
            if not unread and shows_unread:
                failed.append(
                    Failure(
                        f"slide {number}",
                        "no text: its pictures, charts, diagrams or embedded objects are not read",
                    )
                )
                incomplete.add(number)
            continue
        if slide.charts and not (slide.title or slide.shapes or notes):
            # what the slide says of its own is in a chart or diagram, beside the
            # text OCR read in its pictures
            failed.append(
                Failure(f"slide {number}", "its charts, diagrams or embedded objects are not read")
            )
            incomplete.add(number)
        elif shows_unread and not unread:  # a slide failed for what it shows is named there
            not_read.append(number)
        if slide.title:
            acc.heading(1, slide.title, loc, **hidden)
        else:
            acc.close_sections()
        for text, attrs in slide.shapes:
            acc.add("paragraph", text, loc, **attrs, **hidden)
        for text in pictured:
            acc.add("paragraph", text, loc, ocr=True, **hidden)
        if notes:
            acc.add("paragraph", notes, loc, notes=True, **hidden)
    return Artifact(
        format="pptx",
        parser="loom-pptx",
        parser_version=PARSER_VERSION + (f"+tesseract{images.version}" if images.version else ""),
        blocks=tuple(acc.blocks),
        coverage=Coverage("slides", len(parts), len(parts) - len(incomplete), failed=tuple(failed)),
        warnings=(
            (
                "pictures, charts, diagrams or embedded objects on"
                f" {counted(len(not_read), 'slide')} of text are not read: text inside them is"
                " not extracted (attrs.images_unread)",
            )
            if not_read
            else ()
        ),
        attrs={
            "slides": len(parts),
            **({"ocr_slides": ocr_slides} if ocr_slides else {}),
            **({"images_unread": not_read} if not_read else {}),
        },
    )
