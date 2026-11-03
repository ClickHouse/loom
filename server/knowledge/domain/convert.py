"""Markdown from what a document source hands over: a presentation as the slides
API describes it, and an extraction artifact. Pure: no I/O.

A slide becomes one `# Slide N: <title>` section, so the chunker cuts one chunk per
slide: its shape text (groups read through, a table one row per line), then its
speaker notes. A run of bare numbers (a diagram's cell values) says nothing a
reader can search for and is dropped. A slide that is mostly a picture says so,
since its content is not in the text.
"""

from __future__ import annotations

import re
from typing import Any

from server.knowledge.domain.extract import Artifact

# Consecutive bare numeric tokens at which a run is a diagram's cell values, not text.
NUMBER_RUN = 8  # not a setting: a property of the conversion, the same for every source
# Characters of text under which a slide with a picture, chart or video is that picture.
IMAGE_ONLY_CHARS = 120  # not a setting: a property of the conversion
# Characters of a document's first paragraph kept as its description.
DESCRIPTION_CHARS = 300  # not a setting: a description is a line, not a summary

_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)*%?")
_TITLE_TYPES = {"TITLE", "CENTERED_TITLE"}
_PICTURE_KINDS = ("image", "sheetsChart", "video")


def _text_of(text: dict[str, Any] | None) -> str:
    """The plain text of a shape's or cell's text elements."""
    parts = []
    for element in (text or {}).get("textElements") or []:
        run = element.get("textRun") or element.get("autoText") or {}
        parts.append(str(run.get("content") or ""))
    return "".join(parts)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\v", "\n").splitlines() if line.strip()]


def _walk(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every element, the children of groups in place of the group."""
    out: list[dict[str, Any]] = []
    for element in elements or []:
        group = element.get("elementGroup")
        if group is not None:
            out.extend(_walk(group.get("children") or []))
        else:
            out.append(element)
    return out


def _table_lines(table: dict[str, Any]) -> list[str]:
    lines = []
    for row in table.get("tableRows") or []:
        cells = [" ".join(_lines(_text_of(c.get("text")))) for c in row.get("tableCells") or []]
        if any(cells):
            lines.append(" | ".join(cells))
    return lines


def drop_number_runs(lines: list[str], run: int = NUMBER_RUN) -> list[str]:
    """`lines` without every run of `run` or more consecutive bare numbers, counted
    across line breaks; a line left empty is dropped."""
    tokens = [(i, t) for i, line in enumerate(lines) for t in line.split()]
    drop: set[int] = set()
    start = 0
    while start < len(tokens):
        end = start
        while end < len(tokens) and _NUMBER.fullmatch(tokens[end][1]):
            end += 1
        if end - start >= run:
            drop.update(range(start, end))
        start = max(end, start + 1)
    kept: dict[int, list[str]] = {}
    for n, (i, t) in enumerate(tokens):
        if n not in drop:
            kept.setdefault(i, []).append(t)
    return [" ".join(kept[i]) for i in sorted(kept)]


def _notes(slide: dict[str, Any]) -> list[str]:
    """The speaker notes: the text of the notes page's body shape."""
    page = (slide.get("slideProperties") or {}).get("notesPage") or {}
    notes_id = (page.get("notesProperties") or {}).get("speakerNotesObjectId")
    for element in _walk(page.get("pageElements") or []):
        if notes_id and element.get("objectId") == notes_id:
            return _lines(_text_of((element.get("shape") or {}).get("text")))
    return []


def slide_markdown(number: int, slide: dict[str, Any]) -> str:
    """One slide's section."""
    title = ""
    body: list[str] = []
    pictures = 0
    for element in _walk(slide.get("pageElements") or []):
        if any(kind in element for kind in _PICTURE_KINDS):
            pictures += 1
        shape = element.get("shape")
        if shape is not None:
            lines = _lines(_text_of(shape.get("text")))
            kind = (shape.get("placeholder") or {}).get("type")
            if kind in _TITLE_TYPES and not title and lines:
                title, lines = " ".join(lines), []
            body.extend(lines)
        elif "table" in element:
            body.extend(_table_lines(element["table"]))
    body = drop_number_runs(body)
    out = [f"# Slide {number}: {title}".rstrip()]
    out.extend(body)
    notes = _notes(slide)
    if notes:
        out.append("> Notes: " + " ".join(notes))
    if pictures and len(title) + sum(len(line) for line in body) < IMAGE_ONLY_CHARS:
        out.append(f"> [image-only: {pictures} pictures, rationale not extracted]")
    return "\n\n".join(out)


def presentation_markdown(presentation: dict[str, Any]) -> str:
    """Every slide's section, in order."""
    slides = presentation.get("slides") or []
    return "\n\n".join(slide_markdown(n, s) for n, s in enumerate(slides, start=1))


def artifact_markdown(artifact: Artifact) -> str:
    """An extraction artifact's blocks as Markdown: a heading at its depth, every
    other block as its text."""
    parts = []
    for block in artifact.blocks:
        if block.type == "heading":
            parts.append("#" * min(6, max(1, block.depth)) + " " + block.text.strip())
        elif block.text.strip():
            parts.append(block.text.strip())
    return "\n\n".join(parts)


def description_of(markdown: str) -> str:
    """A document's first paragraph that is neither a heading nor a marker, cut short."""
    for paragraph in markdown.split("\n\n"):
        text = " ".join(paragraph.split())
        if text and not text.startswith(("#", ">", "|")):
            return text[:DESCRIPTION_CHARS]
    return ""


__all__ = [
    "artifact_markdown",
    "description_of",
    "drop_number_runs",
    "presentation_markdown",
    "slide_markdown",
]
