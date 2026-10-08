"""Structure-aware chunking for the knowledge plane.

A length window cut across a heading mixes two sections and throws away the
one piece of global structure a document carries for free. Here a document
is first split on its headings (Markdown ATX `#..######`, Setext underlines,
and HTML `<h1>..<h6>`) into sections, each carrying the breadcrumb of
headings above it, and only then windowed *inside* each section with the
configured size and stride. A document with no headings is windowed over its
whole text.

Each chunk records its section path, depth and ordinal in `meta`, which the
ingest path stores as `chunk_metadata` and prepends (as a breadcrumb) to the
text that gets embedded, next to the document title and description. The
stored `content` is the chunk text alone, so snippets read as plain text.

Pure and I/O-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TypedDict

_ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT_H1 = re.compile(r"^=+[ \t]*$")
_SETEXT_H2 = re.compile(r"^-+[ \t]*$")
_HTML_H = re.compile(r"^\s*<h([1-6])[^>]*>(.*?)</h\1>\s*$", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(```|~~~)")
_ANCHOR = re.compile(r"\s*\{#[^}]*\}\s*$")  # `## Title {#anchor}` (Docusaurus/MkDocs)

SEPARATOR = " > "


@dataclass(frozen=True)
class Chunk:
    idx: int
    text: str
    section_path: tuple[str, ...] = ()
    section_depth: int = 0
    section_idx: int = 0
    char_start: int = 0

    @property
    def breadcrumb(self) -> str:
        return SEPARATOR.join(self.section_path)

    @property
    def meta(self) -> dict[str, object]:
        if not self.section_path:
            return {}
        return {
            "section_path": list(self.section_path),
            "section_depth": self.section_depth,
            "section_idx": self.section_idx,
            "char_start": self.char_start,
        }


@dataclass
class Section:
    path: tuple[str, ...]
    depth: int
    start: int  # char offset of the section body (after the heading line)
    end: int = 0  # exclusive
    idx: int = 0
    lines: list[str] = field(default_factory=list)


def _clean_title(raw: str) -> str:
    t = _ANCHOR.sub("", raw).strip()
    t = re.sub(r"<[^>]+>", "", t)  # inline html
    t = re.sub(r"[*_`]+", "", t)  # emphasis / code marks
    return re.sub(r"\s+", " ", t).strip()


def _heading_at(lines: list[str], i: int) -> tuple[int, str, int]:
    """(depth, title, lines consumed) of a heading starting at line `i`: ATX,
    HTML, or setext (the text line plus its underline); depth 0 when the line
    starts none."""
    line = lines[i]
    m = _ATX.match(line)
    if m:
        return len(m.group(1)), _clean_title(m.group(2)), 1
    h = _HTML_H.match(line)
    if h:
        return int(h.group(1)), _clean_title(h.group(2)), 1
    if i + 1 < len(lines) and line.strip() and not line.lstrip().startswith(("-", "*", ">")):
        nxt = lines[i + 1]
        if _SETEXT_H1.match(nxt) and len(nxt.strip()) >= 3:
            return 1, _clean_title(line), 2
        if _SETEXT_H2.match(nxt) and len(nxt.strip()) >= 3:
            return 2, _clean_title(line), 2
    return 0, "", 1


def detect_sections(content: str) -> list[Section]:
    """Split `content` on headings. Returns sections in document order, the
    first being the preamble (empty path) when text precedes the first
    heading. Fenced code blocks are opaque: a `#` inside them is a comment,
    not a heading. Returns [] when the document has no headings at all."""
    lines = content.split("\n")
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (depth, title)
    in_fence: str | None = None
    offset = 0
    cur = Section(path=(), depth=0, start=0)
    saw_heading = False

    def _close(sec: Section, end: int) -> None:
        sec.end = end
        sections.append(sec)

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        line_len = len(line) + 1
        fence = _FENCE.match(line)
        if fence or in_fence is not None:
            if fence:
                marker = fence.group(1)
                if in_fence is None:
                    in_fence = marker
                elif in_fence == marker:
                    in_fence = None
            cur.lines.append(line)
            offset += line_len
            i += 1
            continue

        depth, title, consumed = _heading_at(lines, i)
        if depth and title:
            saw_heading = True
            _close(cur, offset)
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, title))
            heading_len = sum(len(lines[i + k]) + 1 for k in range(consumed))
            cur = Section(path=tuple(t for _, t in stack), depth=depth, start=offset + heading_len)
            offset += heading_len
            i += consumed
            continue

        cur.lines.append(line)
        offset += line_len
        i += 1

    _close(cur, offset)
    if not saw_heading:
        return []
    out = [s for s in sections if s.path or "".join(s.lines).strip()]
    for k, s in enumerate(out):
        s.idx = k
    return out


def window(text: str, *, size: int, stride: int, base: int = 0) -> list[tuple[int, str]]:
    """Fixed windows over `text`, as (char_start, text)."""
    if size <= 0 or stride <= 0 or len(text) <= size:
        return [(base, text)]
    out: list[tuple[int, str]] = []
    pos = 0
    while pos < len(text):
        out.append((base + pos, text[pos : pos + size]))
        if pos + size >= len(text):
            break
        pos += stride
    return out


def chunk_document(
    content: str,
    *,
    size: int,
    stride: int,
) -> list[Chunk]:
    """Chunk a document: split on headings, then window inside each section.
    A document without headings is windowed over its whole text. Chunk
    indices are dense and in document order."""
    sections = detect_sections(content)
    if not sections:
        return [
            Chunk(idx=i, text=t, char_start=s)
            for i, (s, t) in enumerate(window(content, size=size, stride=stride))
        ]
    chunks: list[Chunk] = []
    for sec in sections:
        body = "\n".join(sec.lines)
        # Strip the blank padding around a section body but keep offsets honest.
        lead = len(body) - len(body.lstrip("\n"))
        body = body.strip("\n")
        if not body.strip():
            continue
        for start, text in window(body, size=size, stride=stride, base=sec.start + lead):
            chunks.append(
                Chunk(
                    idx=len(chunks),
                    text=text,
                    section_path=sec.path,
                    section_depth=sec.depth,
                    section_idx=sec.idx,
                    char_start=start,
                )
            )
    if not chunks:  # headings only, no body anywhere: keep the document as one chunk
        return [Chunk(idx=0, text=content)]
    return chunks


class OutlineEntry(TypedDict):
    path: list[str]
    title: str
    depth: int
    chunk_idxs: list[int]


def outline(chunks: list[Chunk]) -> list[OutlineEntry]:
    """The document's table of contents as seen through its chunks: one entry
    per distinct section path, in document order, with chunk indices."""
    seen: dict[tuple[str, ...], OutlineEntry] = {}
    for c in chunks:
        if not c.section_path:
            continue
        entry = seen.get(c.section_path)
        if entry is None:
            entry = OutlineEntry(
                path=list(c.section_path),
                title=c.section_path[-1],
                depth=c.section_depth,
                chunk_idxs=[],
            )
            seen[c.section_path] = entry
        entry["chunk_idxs"].append(c.idx)
    return list(seen.values())


__all__ = [
    "Chunk",
    "Section",
    "OutlineEntry",
    "SEPARATOR",
    "detect_sections",
    "window",
    "chunk_document",
    "outline",
]
