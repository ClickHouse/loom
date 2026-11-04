"""Markdown from what a document source hands over: a presentation as the slides
API describes it, a page store's page as its blocks, and an extraction artifact.
Pure: no I/O.

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


_FENCED = {"code": "", "sql_statement": "sql"}


def _fence(text: str, lang: str) -> str:
    """`text` in a code fence longer than any run of backticks inside it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def artifact_markdown(artifact: Artifact) -> str:
    """An extraction artifact's blocks as Markdown: a heading at its depth, code and
    SQL fenced, every other block as its text."""
    parts = []
    for block in artifact.blocks:
        text = block.text.strip("\n")
        if not text.strip():
            continue
        if block.type == "heading":
            parts.append("#" * min(6, max(1, block.depth)) + " " + text.strip())
        elif block.type in _FENCED:
            parts.append(_fence(text, _FENCED[block.type]))
        else:
            parts.append(text.strip())
    return "\n\n".join(parts)


_HEADINGS = {"heading_1": 1, "heading_2": 2, "heading_3": 3, "heading_4": 4}
_LIST_ITEMS = {"bulleted_list_item", "numbered_list_item", "to_do"}
_BLOCK_PICTURES = {"image", "video"}
_LINKS = {"bookmark", "embed", "link_preview"}
# Blocks that only hold other blocks: their children are read in their place.
_CONTAINERS = {"column_list", "column", "synced_block"}
# Blocks left out without a count: no text of their own, or (a child page) synced as
# a document of its own.
_LAYOUT = {"table_of_contents", "breadcrumb", "child_page"}


def _rich(items: Any) -> str:
    """A rich text array as inline Markdown: emphasis, inline code, equations and
    web links kept."""
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("plain_text") or "")
        marks = item.get("annotations") or {}
        if not text.strip():
            out.append(text)
            continue
        if item.get("type") == "equation":
            text = f"${text}$"
        elif marks.get("code"):
            text = f"`{text}`"
        elif text == text.strip():
            for mark, wrap in (("bold", "**"), ("italic", "*"), ("strikethrough", "~~")):
                if marks.get(mark):
                    text = f"{wrap}{text}{wrap}"
        href = item.get("href")
        if isinstance(href, str) and href.startswith("https://") and item.get("type") == "text":
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out).strip()


def _plain(items: Any) -> str:
    """A rich text array's text as written, for code."""
    return "".join(
        str(i.get("plain_text") or "")
        for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict)
    )


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def _table_markdown(block: dict[str, Any]) -> str:
    """A Markdown table: the first row is its header row, as Markdown needs one, and
    every row has the table's width."""
    rows = []
    for row in block.get("children") or []:
        cells = ((row.get("table_row") or {}).get("cells")) or []
        rows.append([" ".join(_rich(c).split()).replace("|", "\\|") for c in cells])
    if not rows:
        return ""
    width = int((block.get("table") or {}).get("table_width") or 0) or max(map(len, rows))
    lines = ["| " + " | ".join((r + [""] * width)[:width]) + " |" for r in rows]
    lines.insert(1, "|" + " --- |" * width)
    return "\n".join(lines)


def _block_markdown(block: dict[str, Any], number: int, skipped: dict[str, int]) -> str:
    """One block and its children; "" when it holds no text."""
    kind = str(block.get("type") or "")
    found = block.get(kind)
    body: dict[str, Any] = found if isinstance(found, dict) else {}
    if kind == "table":
        return _table_markdown(block)  # its children are its rows
    text = _rich(body.get("rich_text"))
    children = _blocks_markdown(block.get("children") or [], skipped)
    if kind in _HEADINGS:
        head = "#" * _HEADINGS[kind] + " " + text if text else ""
        return "\n\n".join(p for p in (head, children) if p)
    if kind in _LIST_ITEMS:
        if kind == "to_do":
            marker = "- [x] " if body.get("checked") else "- [ ] "
        else:
            marker = f"{number}. " if kind == "numbered_list_item" else "- "
        item = marker + text
        # Nested under the item's text: past the number, or two spaces under a dash.
        indent = len(marker) if kind == "numbered_list_item" else 2
        return item + ("\n" + _indent(children, " " * indent) if children else "")
    if kind in ("quote", "callout"):
        icon = body.get("icon") or {}
        if kind == "callout" and icon.get("type") == "emoji" and icon.get("emoji"):
            text = f"{icon['emoji']} {text}".strip()
        inner = "\n\n".join(p for p in (text, children) if p)
        return _indent(inner, "> ").replace("\n\n", "\n>\n") if inner else ""
    if kind == "code":
        code = _plain(body.get("rich_text")).strip("\n")
        lang = str(body.get("language") or "")
        return _fence(code, "" if lang == "plain text" else lang) if code.strip() else ""
    if kind == "equation":
        expression = str(body.get("expression") or "").strip()
        return f"$$\n{expression}\n$$" if expression else ""
    if kind == "divider":
        return "---"
    if kind in _LINKS:
        url = str(body.get("url") or "")
        if url.startswith("https://"):
            return f"[{_rich(body.get('caption')) or url}]({url})"
        return _skip(kind, skipped)  # not a web address
    if kind == "synced_block" and block.get("unreadable"):
        return _skip(kind, skipped)  # its original is not shared with the integration
    if kind in ("paragraph", "toggle") or kind in _CONTAINERS:
        return "\n\n".join(p for p in (text, children) if p)
    return "" if kind in _LAYOUT else _skip(kind or "unknown", skipped)


def _skip(kind: str, skipped: dict[str, int]) -> str:
    """Count a block left out of the Markdown."""
    skipped[kind] = skipped.get(kind, 0) + 1
    return ""


def _picture_marker(pictures: int, captions: list[str]) -> str:
    noun = "picture" if pictures == 1 else "pictures"
    marker = f"> [image-only: {pictures} {noun}, rationale not extracted]"
    return "\n\n".join([marker, *captions])


def _blocks_markdown(blocks: list[dict[str, Any]], skipped: dict[str, int]) -> str:
    """Blocks in order; consecutive list items one per line, a run of pictures one
    marker."""
    parts: list[tuple[str, bool]] = []
    pictures: list[str] = []
    seen_pictures = 0
    number = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind in _BLOCK_PICTURES:
            seen_pictures += 1
            caption = _rich((block.get(kind) or {}).get("caption"))
            if caption:
                pictures.append(caption)
            continue
        if seen_pictures:
            parts.append((_picture_marker(seen_pictures, pictures), False))
            seen_pictures, pictures = 0, []
        number = number + 1 if kind == "numbered_list_item" else 0
        text = _block_markdown(block, number, skipped)
        if text:
            parts.append((text, kind in _LIST_ITEMS))
    if seen_pictures:
        parts.append((_picture_marker(seen_pictures, pictures), False))
    out = ""
    for i, (text, listed) in enumerate(parts):
        if i:
            out += "\n" if listed and parts[i - 1][1] else "\n\n"
        out += text
    return out


def page_markdown(blocks: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    """A page store's page, its blocks as the blocks API lists them (each block's
    children under `children`), as Markdown one to one: headings, paragraphs, lists,
    to-dos, quotes, callouts, code fenced, tables, and toggles and columns with their
    children inline. A run of pictures says so, since its content is not in the text.
    Also the block types left out, by count: child databases, files, links that are
    not web addresses, synced copies whose original cannot be read and any type not
    mapped here. A child page is left out without a count: it is a document of its
    own (`child_page_ids`)."""
    skipped: dict[str, int] = {}
    return _blocks_markdown(blocks, skipped), skipped


def child_page_ids(blocks: list[dict[str, Any]]) -> list[str]:
    """The ids of the child pages among `blocks` and their children, in order."""
    found: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "child_page" and block.get("id"):
            found.append(str(block["id"]))
        found.extend(child_page_ids(block.get("children") or []))
    return found


def coverage_of(artifact: Artifact) -> dict[str, Any]:
    """How much of the original the artifact holds, for the document's metadata, so
    a partial extraction is never read as the whole file."""
    c = artifact.coverage
    return {
        "complete": c.complete,
        "unit": c.unit,
        "total": c.total,
        "extracted": c.extracted,
        "lost": list(c.lost),
    }


def description_of(markdown: str) -> str:
    """A document's first paragraph that is neither a heading nor a marker, cut short."""
    for paragraph in markdown.split("\n\n"):
        text = " ".join(paragraph.split())
        if text and not text.startswith(("#", ">", "|")):
            return text[:DESCRIPTION_CHARS]
    return ""


__all__ = [
    "artifact_markdown",
    "child_page_ids",
    "coverage_of",
    "description_of",
    "drop_number_runs",
    "page_markdown",
    "presentation_markdown",
    "slide_markdown",
]
