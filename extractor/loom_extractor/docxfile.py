"""A DOCX file's body as blocks, read from its XML parts with the standard
library: headings by their style (a `Heading N` or `Title` style, or an
outline level on the paragraph or its style chain; the other headings after
a title sit one level under their own), list paragraphs (a numbered
paragraph or a `List` style) as one list block per run, other paragraphs,
tables (one block each, a row per line and its cells joined by ` | `,
`attrs.table` its shape), then footnotes and endnotes.

Read as the document holds it: inserted text is kept, and a tracked deletion
(its runs, rows and cells) and moved text at its old place left out, a content
control's or a custom XML element's text is read in place (in the body, a
table's rows and its cells), and an alternate-content choice is read once (its
fallback copy is skipped). Headers, footers and comments are not the
document's body and are not read. An embedded document (`altChunk`) is a
failed unit, as is a document of pictures and no text: what it says is in
them. The body is one part, so a body nested past what the reader walks fails
the file. A DOCX has no pages of its own (pagination is the renderer's), so a
block's locator is empty and its section path is what a citation names.

Pure and I/O-free.
"""

from __future__ import annotations

import re
from xml.etree.ElementTree import Element

from loom_extractor.artifact import Artifact, Blocks, Coverage, Failure, Locator
from loom_extractor.ooxml import Package, PartError, q

PARSER_VERSION = "3"  # a document's headings sit under its Title paragraph
_MAIN = "word/document.xml"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_PICT = q("w:pict")
# What the document no longer holds, or holds twice: a tracked deletion (its runs
# keep their tabs, breaks and pictures), the old place of moved text (a tracked move
# keeps its text in ordinary runs at both places), an alternate-content fallback copy.
_NOT_READ = {q("w:del"), q("w:moveFrom"), _MC_FALLBACK}
_HEADING_NAME = re.compile(r"heading ([1-9])")
# How far a style's `basedOn` chain is followed for an outline level.
_MAX_BASED_ON = 16  # not a setting: a bound on hostile input
_NO_LOC = Locator()
# An outline level, `0` to `8`, and the heading depth it makes; `9` sets body text
# (no heading, whatever the style says), and any other value sets no level (looked
# up, so no hostile number is parsed).
_OUTLINE = {str(i): i + 1 for i in range(9)}
_BODY_TEXT = "9"


def _val(el: Element | None, tag: str) -> str:
    """The `w:val` of `el`'s child `tag`, or ''."""
    child = el.find(q(tag)) if el is not None else None
    return child.get(q("w:val"), "") if child is not None else ""


def _heading_levels(pkg: Package) -> tuple[dict[str, int], set[str]]:
    """Each paragraph style id that makes a heading, and its depth, and the ids
    of the `Title` styles."""
    if not pkg.has("word/styles.xml"):
        return {}, set()
    styles: dict[str, tuple[str, str, str]] = {}  # id: (name, outline level, based on)
    for s in pkg.xml("word/styles.xml").iter(q("w:style")):
        if s.get(q("w:type")) == "paragraph":
            styles[s.get(q("w:styleId"), "")] = (
                _val(s, "w:name").lower(),
                _val(s.find(q("w:pPr")), "w:outlineLvl"),
                _val(s, "w:basedOn"),
            )
    levels = {}
    titles: set[str] = set()
    for sid, (name, own, _) in styles.items():
        m = _HEADING_NAME.fullmatch(name)
        if own == _BODY_TEXT:
            continue  # a style set to body text is no heading, whatever its name
        if m:
            levels[sid] = int(m.group(1))
        elif name == "title":
            levels[sid] = 1
            titles.add(sid)
        else:
            cur, hops = sid, 0
            while cur in styles and hops < _MAX_BASED_ON:
                lvl = styles[cur][1]
                if lvl == _BODY_TEXT:
                    break  # the nearest level set says body text
                if lvl in _OUTLINE:
                    levels[sid] = _OUTLINE[lvl]
                    break
                cur, hops = styles[cur][2], hops + 1
    return levels, titles


def _text(el: Element) -> str:
    """`el`'s text in document order: runs, tabs and breaks; a tracked deletion,
    moved text at its old place and an alternate-content fallback are not read."""
    out: list[str] = []

    def walk(node: Element) -> None:
        for child in node:
            tag = child.tag
            if tag in _NOT_READ:
                continue
            if tag == q("w:t"):
                out.append(child.text or "")
            elif tag == q("w:tab"):
                out.append("\t")
            elif tag in (q("w:br"), q("w:cr")):
                out.append("\n")
            elif tag == q("w:noBreakHyphen"):
                out.append("-")
            elif tag == q("w:p") and out and out[-1] != "\n":
                out.append("\n")  # a text box's paragraph inside this one
                walk(child)
            else:
                walk(child)

    walk(el)
    return "".join(out).strip()


def _children(el: Element, tag: str) -> list[Element]:
    """`el`'s children of `tag`, those inside a content control (`w:sdt`) or a
    custom XML element included, in document order."""
    out = []
    for child in el:
        if child.tag == tag:
            out.append(child)
        elif child.tag == q("w:sdt"):
            content = child.find(q("w:sdtContent"))
            if content is not None:
                out.extend(_children(content, tag))
        elif child.tag == q("w:customXml"):
            out.extend(_children(child, tag))
    return out


def _deleted(el: Element, props: str, mark: str) -> bool:
    """Whether a row or cell is a tracked deletion (`w:trPr/w:del`, `w:tcPr/w:cellDel`)."""
    pr = el.find(q(props))
    return pr is not None and pr.find(q(mark)) is not None


def _draws(el: Element) -> bool:
    """Whether `el` holds a picture or a drawing the document still holds."""
    for child in el:
        if child.tag in _NOT_READ:
            continue
        if child.tag in (q("w:drawing"), _PICT) or _draws(child):
            return True
    return False


def _table(tbl: Element) -> tuple[str, int, int]:
    rows = []
    for tr in _children(tbl, q("w:tr")):
        if _deleted(tr, "w:trPr", "w:del"):
            continue
        cells = [tc for tc in _children(tr, q("w:tc")) if not _deleted(tc, "w:tcPr", "w:cellDel")]
        rows.append([" ".join(_text(tc).split()) for tc in cells])
    columns = max((len(r) for r in rows), default=0)
    return "\n".join(" | ".join(r) for r in rows), len(rows), columns


class _Body:
    def __init__(self, levels: dict[str, int], titles: set[str]) -> None:
        self.acc = Blocks()
        self.levels = levels
        self.titles = titles
        self.shift = 0  # 1 once a title heading is read: the headings after it sit under it
        self.items: list[str] = []  # the open list's paragraphs
        self.units = 0  # paragraphs with text, and tables
        self.failed: list[Failure] = []
        self.drawings = False  # whether a paragraph holds a picture or a drawing

    def close_list(self) -> None:
        if self.items:
            self.acc.add("list", "\n".join(self.items), _NO_LOC)
            self.items = []

    def paragraph(self, p: Element) -> None:
        text = _text(p)
        if _draws(p):
            self.drawings = True
        if not text:
            return
        self.units += 1
        ppr = p.find(q("w:pPr"))
        style = _val(ppr, "w:pStyle")
        own = _val(ppr, "w:outlineLvl")
        depth = 0 if own == _BODY_TEXT else _OUTLINE.get(own) or self.levels.get(style, 0)
        if depth and style in self.titles:
            # a title is the document's top, whatever outline level it carries
            depth, self.shift = 1, 1
        elif depth:
            depth += self.shift
        if depth:
            self.close_list()
            self.acc.heading(depth, " ".join(text.split()), _NO_LOC)
        elif (ppr is not None and ppr.find(q("w:numPr")) is not None) or "list" in style.lower():
            self.items.append(text)
        else:
            self.close_list()
            self.acc.add("paragraph", text, _NO_LOC)

    def table(self, tbl: Element) -> None:
        self.close_list()
        if _draws(tbl):
            self.drawings = True
        text, rows, columns = _table(tbl)
        if text.strip(" |\n"):
            self.units += 1
            self.acc.add("paragraph", text, _NO_LOC, table={"rows": rows, "columns": columns})

    def read(self, container: Element) -> None:
        for child in container:
            if child.tag == q("w:p"):
                self.paragraph(child)
            elif child.tag == q("w:tbl"):
                self.table(child)
            elif child.tag == q("w:sdt"):
                content = child.find(q("w:sdtContent"))
                if content is not None:
                    self.read(content)
            elif child.tag == q("w:customXml"):
                self.read(child)
            elif child.tag == q("w:altChunk"):
                # Another document embedded whole (HTML, RTF, a second DOCX): not read.
                self.failed.append(Failure("embedded document", "an altChunk is not read"))
        self.close_list()

    def notes(self, pkg: Package, part: str, kind: str) -> None:
        if not pkg.has(part):
            return
        try:
            root = pkg.xml(part)
        except PartError as e:
            self.failed.append(Failure(f"{kind}s", str(e)))
            return
        for note in root.iter(q(f"w:{kind}")):
            if note.get(q("w:type"), "normal") != "normal":
                continue  # the separator lines between body and notes
            text = _text(note)
            if text:
                self.units += 1
                self.acc.add("paragraph", text, _NO_LOC, note=kind)


def extract_docx(data: bytes) -> Artifact:
    pkg = Package(data)
    body = pkg.xml(_MAIN).find(q("w:body"))
    try:
        (levels, titles), styles_failed = _heading_levels(pkg), None
    except PartError as e:  # the headings a style makes are lost, not the text
        levels, titles, styles_failed = {}, set(), Failure("styles", str(e))
    reader = _Body(levels, titles)
    if styles_failed:
        reader.failed.append(styles_failed)
    if body is not None:
        reader.read(body)
    reader.notes(pkg, "word/footnotes.xml", "footnote")
    reader.notes(pkg, "word/endnotes.xml", "endnote")
    if not reader.units and reader.drawings:
        # Pictures and no text, like a slide of pictures alone: what it says is in them.
        reader.failed.append(Failure("document", "no text: its pictures are not read"))
    failed = tuple(reader.failed)
    return Artifact(
        format="docx",
        parser="loom-docx",
        parser_version=PARSER_VERSION,
        blocks=tuple(reader.acc.blocks),
        coverage=Coverage("paragraphs", reader.units + len(failed), reader.units, failed=failed),
    )
