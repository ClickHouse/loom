"""Structure-aware chunking for the knowledge plane.

A length window cut across a heading mixes two sections and throws away the
one piece of global structure a document carries for free. Here a document
is first split on its headings (Markdown ATX `#..######`, Setext underlines,
and HTML `<h1>..<h6>`) into sections, each carrying the breadcrumb of
headings above it, and only then windowed *inside* each section with the
configured size and stride. A document with no headings is windowed over its
whole text.

Each chunk records its section path, depth and ordinal in `meta`, which the
ingest path stores as `chunk_metadata`. Its breadcrumb, with the document title
and description, is the chunk's context (`chunk_context`): stored as the
chunk's `context`, it heads the embedded text and is scored by the lexical leg.
The stored `content` is the chunk text alone, so snippets read as plain text.

Pure and I/O-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TypedDict

# The closing `#` run is stripped in `_without_closing_hashes` (a pattern tail
# backtracks on long blanks); a bare `#` is an empty heading, `#hashtag` none.
# Indented up to three spaces.
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*\r?)$")
# Indented up to three spaces (a tab is four columns); `\r?`: a CRLF line keeps its `\r`.
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*\r?$")
_HTML_H = re.compile(r"^\s*<h([1-6])[^>]*>(.*?)</h\1>\s*$", re.IGNORECASE)
_FENCE_RUN = re.compile(r"^\s*(`{3,}|~{3,})")


def _fence_marker(line: str) -> str:
    """The fence `line` opens (its run of backticks or tildes), or ''. A backtick
    fence's info string holds no backtick: "```js``` is inline" opens nothing.
    Checked after the match, not by a lookahead the run would backtrack into."""
    m = _FENCE_RUN.match(line)
    if m is None or (m.group(1)[0] == "`" and "`" in line[m.end() :]):
        return ""
    return m.group(1)


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
    def keys(self) -> tuple[str, ...]:
        """A text chunk has no exact lookup keys."""
        return ()

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


def chunk_context(title: str, description: str, breadcrumb: str) -> str:
    """What a chunk is about beyond its own text: the document title, description and
    the chunk's section breadcrumb, one per line, the empty ones left out. Both search
    legs read it: it heads the embedded text and the lexical leg scores it. A first
    heading that repeats the title stays; the repeat acts as a title boost."""
    parts = [p.strip() for p in (title, description, breadcrumb)]
    return "\n".join(p for p in parts if p)


def embedding_text(context: str, text: str) -> str:
    """The text a chunk's vector is computed from: its context, a blank line, its text."""
    return f"{context}\n\n{text}" if context else text


@dataclass
class Section:
    path: tuple[str, ...]
    depth: int
    start: int  # char offset of the section body (after the heading line)
    end: int = 0  # exclusive
    idx: int = 0
    lines: list[str] = field(default_factory=list)


# The inline HTML a heading carries; any other `<...>` (`<your token>`,
# `Promise<T extends Base>`) is text.
_INLINE = "a|abbr|b|br|code|del|em|i|img|ins|kbd|mark|picture|s|small|source|span|strong|sub|sup|u"
# Lowercase only, as Markdown writers spell tags: `Into<S>` and `Option<A>` are text.
# A tag name ends at a space, `/` or `>`: `Map<a, b>` is text.
_PAIRED_TAG = re.compile(rf"<({_INLINE})(?=[\s/>])[^>]*>([^<]*)</\1>")
_LONE_TAG = re.compile(rf"</?(?:{_INLINE})(?=[\s/>])[^>]*>")
# An autolink (`<https://example.com>`): its URL is the text.
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>")
# A code span opens and closes on backtick runs of one length: ``a`b`` is `a`b`.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# A marker alone on its line (`-`, `1.`) is an empty item.
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])(?:\s+|$)")
# The tags that open an HTML block (CommonMark's block start condition 6).
_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|"
    "details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|"
    "h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|"
    "optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|"
    "tr|track|ul"
)
# The start of an HTML block, by CommonMark's start condition: 1-5 (a raw-text
# tag, `<!--`, `<?`, `<![CDATA[`, `<!X`) end on the line holding their end
# marker, 6 (a block-level tag) and 7 (a line holding one open or closing tag)
# at a blank line. A tag name is CommonMark's (letters, digits, `-`: `<YOUR_KEY>`
# is text); its attributes are one `[^>]*` run, so the pattern stays linear.
_HTML_OPEN = re.compile(
    r"^ {0,3}(?:<(?P<raw>pre|script|style|textarea)(?=[\s>]|$)|(?P<comment><!--)"
    r"|(?P<pi><\?)|(?P<cdata><!\[CDATA\[)|(?P<decl><![A-Za-z])"
    rf"|(?P<block></?(?:{_BLOCK_TAGS})(?=[\s/>]|$))"
    r"|(?P<tags></?[A-Za-z][A-Za-z0-9-]*(?:[\s/][^>]*)?>[ \t]*\r?$))",
    re.IGNORECASE,
)
_HTML_END = {"comment": "-->", "pi": "?>", "cdata": "]]>", "decl": ">"}


def _html_block(line: str) -> tuple[str, bool] | None:
    """(the text that ends the HTML block `line` opens, on this line or a later
    one, '' for a blank line; whether it may interrupt a paragraph, which a line
    of only tags may not), or None when `line` opens none."""
    m = _HTML_OPEN.match(line)
    if not m:
        return None
    kind = m.lastgroup or ""
    if kind == "raw":
        return f"</{m.group('raw').lower()}>", True
    return _HTML_END.get(kind, ""), kind != "tags"


# A thematic break (`***`, `- - -`, `___`): a rule, never a heading's title.
_THEMATIC_BREAK = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*\r?$")
# A matched `*`/`**` pair: a lone `*` (`2 * 3`, `*args`) stays, and underscores
# are left alone, since `__init__` and `max_rows` are names far more often than emphasis.
_EMPHASIS = re.compile(r"(?<![\w*])(\*{1,3})(?=\S)([^*]+?)(?<=\S)\1(?![\w*])")
# Characters of a heading line its markup is read in (the rest is kept as written),
# so the title patterns above stay bounded.
_TITLE_MAX = 500  # not a setting: a bound on hostile input, not a tuning knob


def _clean_title(raw: str) -> str:
    """A heading's text without its markup: inline HTML tags (not other `<...>`
    text), code backticks (not the code), and matched `*` emphasis outside code
    (not a lone `*`, and never an underscore), and a trailing heading-id
    attribute (`{#anchor}`). The markup is read in the first `_TITLE_MAX`
    characters; the rest is kept as written."""
    text = _without_anchor(raw.strip())
    head, rest = text[:_TITLE_MAX], text[_TITLE_MAX:]
    # Code spans stand aside as placeholders while the markup around them goes,
    # so `**The `foo` option**` loses its emphasis and keeps `foo` as written.
    codes: list[str] = []

    def aside(m: re.Match[str]) -> str:
        codes.append(m.group(2))
        return f"\x00{len(codes) - 1}\x00"

    cleaned = _markup_out(_CODE_SPAN.sub(aside, head.replace("\x00", "")))
    cleaned = re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], cleaned)
    return " ".join((cleaned + rest).split())


def _without_anchor(text: str) -> str:
    """`Title {#anchor}` without its anchor."""
    start = text.rfind("{#")
    if text.endswith("}") and start >= 0 and "}" not in text[start:-1]:
        return text[:start].rstrip()
    return text


def _without_closing_hashes(body: str) -> str:
    """An ATX heading's text: without trailing blanks or `\r`, nor a closing `#`
    run that a blank precedes (`## Closed ##`; `## Using C#` keeps its `#`)."""
    body = body.rstrip(" \t\r")
    bare = body.rstrip("#")
    if bare != body and (not bare or bare[-1] in " \t"):
        body = bare.rstrip(" \t")
    return body


def _markup_out(text: str) -> str:
    """Inline HTML, autolink brackets and `*` emphasis out of heading text outside
    code."""
    text = _AUTOLINK.sub(r"\1", text)
    text = _LONE_TAG.sub(" ", _PAIRED_TAG.sub(r"\2", text))
    # Twice: the inner pair of `**a *b* c**` goes first, then the outer.
    return _EMPHASIS.sub(r"\2", _EMPHASIS.sub(r"\2", text))


def _closes_fence(line: str, opener: str) -> bool:
    """Whether `line` closes the fence `opener` opened: a run of the same
    character at least as long, and nothing else on the line."""
    run = line.strip()
    return len(run) >= len(opener) and set(run) == {opener[0]}


def _atx_line(line: str) -> tuple[int, str] | None:
    """(depth, text before cleanup) of an ATX heading line, the text `''` for an
    empty heading such as `# #`; None when `line` is no ATX heading."""
    m = _ATX.match(line)
    return (len(m.group(1)), _without_closing_hashes(m.group(2) or "")) if m else None


def _heading_at(lines: list[str], i: int) -> tuple[int, str, int]:
    """(depth, title, lines consumed) of a heading starting at line `i`: ATX,
    HTML, or setext (the text line plus its underline); depth 0 when the line
    starts none."""
    line = lines[i]
    atx = _atx_line(line)
    if atx is not None:  # an ATX line, even an empty one (`# #`), is never a setext title
        depth, body = atx
        return (depth, _clean_title(body), 1) if body else (0, "", 1)
    h = _HTML_H.match(line)
    if h:
        return int(h.group(1)), _clean_title(h.group(2)), 1
    # The underline first, which almost no line has; then a list item, a quote, a
    # rule or an HTML block's line over it is that, not a heading's title.
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    if (
        _SETEXT_UNDERLINE.match(nxt)
        and len(nxt.strip()) >= 3  # unlike CommonMark: `Title` over `--` is text
        and line.strip()
        and not _LIST_ITEM.match(line)
        and not _THEMATIC_BREAK.match(line)
        and _html_block(line) is None
        # A comment opened inside a paragraph is not a block and is not tracked, so a
        # line ending one (`Intro <!-- a` ... `b -->`) is refused: a heuristic, as
        # CommonMark would take a plain `Next step -->` over `---` as a title.
        and not line.rstrip().endswith("-->")
        and not line.lstrip().startswith(">")
    ):
        return (1 if nxt.strip()[0] == "=" else 2), _clean_title(line), 2
    return 0, "", 1


class _Opaque:
    """Which lines are inside a fenced code block or an HTML block, where a `#`
    or an underline is text, never a heading (CommonMark's block rules)."""

    def __init__(self) -> None:
        self.fence: str | None = None
        # What ends the open HTML block: its end marker, or '' for a blank line.
        self.html: str | None = None
        self.after_text = False  # a paragraph line before, which a tag-only line cannot interrupt

    def takes(self, line: str) -> bool:
        """Whether `line` belongs to an open block, or opens a fence."""
        if self.html is not None:
            ends = self.html in line.lower() if self.html else not line.strip()
            if ends:
                self.html = None
        else:
            fence = _fence_marker(line)
            if not fence and self.fence is None:
                return False
            if self.fence is None:
                self.fence = fence
            elif _closes_fence(line, self.fence):
                self.fence = None
        self.after_text = False
        return True

    def text(self, line: str) -> None:
        """`line` is no heading: it may open an HTML block."""
        opened = _html_block(line)
        if opened is not None and (opened[1] or not self.after_text):
            end = opened[0]
            # A block ends on its opening line when its end marker is there too.
            self.html = None if end and end in line.lower() else end
            self.after_text = False
            return
        # Only a paragraph's line: a rule or an empty `#` line ends what is above it.
        self.after_text = (
            bool(line.strip()) and not _THEMATIC_BREAK.match(line) and _atx_line(line) is None
        )

    def heading(self, line: str) -> None:
        """`line` started a heading. A one-line `<hN>` opens a block-tag HTML
        block in CommonMark, which runs to the next blank line."""
        self.after_text = False
        if _HTML_H.match(line):
            self.html = ""


def detect_sections(content: str) -> list[Section]:
    """Split `content` on headings. Returns sections in document order, the
    first being the preamble (empty path) when text precedes the first
    heading. Fenced code blocks and HTML blocks are opaque: a `#` inside them is
    text, not a heading. Returns [] when the document has no headings at all."""
    lines = content.split("\n")
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (depth, title)
    opaque = _Opaque()
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
        if not opaque.takes(line):
            depth, title, consumed = _heading_at(lines, i)
            if depth and title:
                saw_heading = True
                _close(cur, offset)
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                stack.append((depth, title))
                heading_len = sum(len(lines[i + k]) + 1 for k in range(consumed))
                cur = Section(
                    path=tuple(t for _, t in stack), depth=depth, start=offset + heading_len
                )
                opaque.heading(line)
                offset += heading_len
                i += consumed
                continue
            opaque.text(line)
        cur.lines.append(line)
        offset += len(line) + 1
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
    "chunk_context",
    "detect_sections",
    "embedding_text",
    "window",
    "chunk_document",
    "outline",
]
