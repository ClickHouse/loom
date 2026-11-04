"""A PPTX deck as blocks, read from its XML parts with the standard library:
slide by slide in the deck's order, cited by slide number. A slide's title is
a heading its other blocks sit under; each other text shape is a block, in the
slide's order (grouped shapes in place), a table one block (a row per line,
its cells joined by ` | `, `attrs.table` its shape), and the speaker notes a
block marked `attrs.notes`. A hidden slide is read and marked `attrs.hidden`.

A shape PowerPoint wraps in alternate content (an equation, a newer shape
kind) is read through its first choice. A slide that holds pictures, charts,
diagrams or embedded objects and no text is a failed unit: what it shows is in
them, which this parser does not read.

Pure and I/O-free.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element

from loom_extractor.artifact import Artifact, Blocks, Coverage, Failure, Locator
from loom_extractor.ooxml import Package, PartError, q

PARSER_VERSION = "1"
_PRESENTATION = "ppt/presentation.xml"
_TITLES = {"title", "ctrTitle"}
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


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


class _Slide:
    """One slide's title, its text shapes and tables in order, and whether it
    holds what is not read (a picture, a chart, a diagram, an embedded object)."""

    def __init__(self, root: Element) -> None:
        self.title = ""
        self.shapes: list[tuple[str, dict[str, object]]] = []  # (text, attrs)
        self.unread = False
        tree = root.find(f"{q('p:cSld')}/{q('p:spTree')}")
        if tree is not None:
            self._walk(tree)

    def _walk(self, tree: Element) -> None:
        for el in tree:
            if el.tag == q("p:grpSp"):
                self._walk(el)
            elif el.tag == f"{_MC}AlternateContent":
                choice = el.find(f"{_MC}Choice")
                if choice is not None:
                    self._walk(choice)
            elif el.tag == q("p:pic"):
                self.unread = True
            elif el.tag == q("p:sp"):
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
                    self.unread = True  # a chart, a diagram or an embedded object
                for tbl in tables:
                    text, rows, columns = _table(tbl)
                    if text.strip(" |\n"):
                        self.shapes.append((text, {"table": {"rows": rows, "columns": columns}}))


def _notes(pkg: Package, slide_part: str) -> str:
    for kind, target in pkg.rels(slide_part).values():
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


def extract_pptx(data: bytes) -> Artifact:
    pkg = Package(data)
    parts = _slide_parts(pkg)
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
        try:
            notes = _notes(pkg, part)
        except (PartError, RecursionError) as e:  # the slide is still read; its notes are not
            failed.append(Failure(f"slide {number} notes", str(e)))
            incomplete.add(number)
            notes = ""
        # `show` is an XML boolean.
        hidden = {"hidden": True} if root.get("show") in ("0", "false") else {}
        if not (slide.title or slide.shapes or notes):
            if slide.unread:
                failed.append(
                    Failure(
                        f"slide {number}", "no text: its pictures, charts or diagrams are not read"
                    )
                )
                incomplete.add(number)
            continue
        if slide.title:
            acc.heading(1, slide.title, loc, **hidden)
        else:
            acc.close_sections()
        for text, attrs in slide.shapes:
            acc.add("paragraph", text, loc, **attrs, **hidden)
        if notes:
            acc.add("paragraph", notes, loc, notes=True, **hidden)
    return Artifact(
        format="pptx",
        parser="loom-pptx",
        parser_version=PARSER_VERSION,
        blocks=tuple(acc.blocks),
        coverage=Coverage("slides", len(parts), len(parts) - len(incomplete), failed=tuple(failed)),
        attrs={"slides": len(parts)},
    )
