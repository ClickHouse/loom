"""Markdown, HTML and plain text into artifact blocks.

Markdown keeps its headings (ATX, setext, a one-line `<hN>`), paragraphs,
lists and fenced code blocks, each with its line span and character offsets;
a code block's span is its body, without the fence lines. HTML keeps the same
block kinds from its elements, with line spans, closing a block whose end tag
HTML lets the writer leave out; script, style and other invisible elements are
dropped. Plain text is paragraphs: a `#` line in a log is not a heading.

Pure and I/O-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from loom_extractor.artifact import (
    Artifact,
    Block,
    Blocks,
    BlockType,
    Coverage,
    Failure,
    Lines,
    Locator,
    on_python,
)
from loom_extractor.markdown import (
    LIST_ITEM,
    SETEXT_UNDERLINE,
    THEMATIC_BREAK,
    atx_body,
    clean_title,
    closes_fence,
    fence_marker,
    heading_at,
    html_block,
)

# Bumped whenever these parsers' output changes, including through the heading,
# fence and title rules in `markdown`, so a changed artifact is a new one.
PARSER_VERSION = "1"

# A list item that may interrupt a paragraph (CommonMark): a bullet, or an ordered
# item numbered 1, with text. A wrapped line that starts "2019. The" stays prose.
_INTERRUPTS = re.compile(r"^\s*(?:[-*+]|1[.)])\s+\S")


# A line of YAML front matter: `key: value`, a `- item`, a comment, or an indented continuation.
# A `#` at the line's start followed by a blank is a heading (`---` then `# Agenda`
# opens a deck, not front matter); an indented or unspaced `#` is a YAML comment.
_YAML_LINE = re.compile(r"^(?:[\w\"'.-]+\s*:|\s*-\s|#(?![ \t])|\s+\S)")
_YAML_KEY = re.compile(r"^[\w\"'.-]+\s*:")


class _Lines(Lines):
    """The text's lines, and the locator of a run of them."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.lines = text.split("\n")
        self.markup: set[int] = set()  # lines that are markup, not text (0-based)

    def loc(self, first: int, last: int) -> Locator:
        return Locator(
            line_start=first + 1,
            line_end=last + 1,
            char_start=self.start(first),
            char_end=self.start(last) + len(self.lines[last]),
        )

    def coverage(self, blocks: list[Block]) -> Coverage:
        """Non-blank lines, and how many land in a block's span or are markup (a
        fence line, a front-matter delimiter, a thematic break, an empty ATX
        heading): a guard against a parser change that drops a line, not a
        measure of loss within a block."""
        covered = set(self.markup)
        for b in blocks:
            covered.update(range(b.locator.line_start - 1, b.locator.line_end))
        nonblank = [i for i, line in enumerate(self.lines) if line.strip()]
        return Coverage("lines", len(nonblank), sum(1 for i in nonblank if i in covered))


def _fence(src: _Lines, acc: Blocks, i: int, marker: str) -> int:
    """The fenced code block opening on line `i`; returns the line after it."""
    lines, n = src.lines, len(src.lines)
    j = i + 1
    while j < n and not closes_fence(lines[j], marker):
        j += 1
    src.markup.update(x for x in (i, j) if x < n)  # the fences
    info = lines[i].strip().lstrip(marker[0]).split(maxsplit=1)
    first, last = i + 1, (j - 1 if j < n else n - 1)
    while j >= n and last >= first and not lines[last].strip():
        last -= 1  # an unclosed fence ends on its last text, not the file's trailing newline
    # `js title="a.js"` names the language js; the rest of the line is kept too.
    attrs = dict(zip(("language", "info"), info, strict=False))
    if first <= last:
        acc.add("code", "\n".join(lines[first : last + 1]), src.loc(first, last), **attrs)
    elif attrs:  # an empty block still holds its info string
        acc.add("code", "", src.loc(i, i), **attrs)
    return j + 1


def _front_matter(src: _Lines, acc: Blocks) -> int:
    """A leading `---` ... `---` block (YAML front matter of documentation
    pages) as one block; returns the line after it, 0 when there is none."""
    lines = src.lines
    if not lines or lines[0].strip() != "---":
        return 0
    for j in range(1, len(lines)):
        if lines[j].strip() in ("---", "..."):
            body = [x for x in lines[1:j] if x.strip()]
            if not all(_YAML_LINE.match(x) for x in body) or (
                body and not any(_YAML_KEY.match(x) for x in body)
            ):
                return 0  # a thematic break (`---` then `# Slide 1`), not front matter
            if body:
                acc.add("paragraph", "\n".join(lines[1:j]), src.loc(1, j - 1), front_matter=True)
            src.markup.update((0, j))
            return j + 1
    return 0


def _html_lines(src: _Lines, acc: Blocks, i: int, end: str) -> int:
    """An HTML block from line `i` as one paragraph, as written: to the line
    holding `end` (line `i` itself too, so `<!-->` is a whole comment), or the
    file's end; to the next blank line when `end` is ''. Returns the line after it."""
    lines, n = src.lines, len(src.lines)
    j = i
    if end:
        while j < n - 1 and end not in lines[j].lower():
            j += 1
    else:
        while j < n - 1 and lines[j + 1].strip():
            j += 1
    acc.add("paragraph", "\n".join(lines[i : j + 1]), src.loc(i, j))
    return j + 1


def _paragraph_end(lines: list[str], i: int, kind: BlockType) -> tuple[int, int]:
    """(the line after the paragraph or list item starting at line `i`, the
    depth of the setext heading it is the title of, or 0). A quote over a rule
    is no heading; an HTML block other than a line of only tags interrupts it."""
    n = len(lines)
    quoted = lines[i].lstrip().startswith(">")
    j = i + 1
    while j < n and lines[j].strip() and not fence_marker(lines[j]):
        underline = SETEXT_UNDERLINE.match(lines[j]) and len(lines[j].strip()) >= 3
        if kind == "paragraph" and underline and not quoted:
            return j, 1 if lines[j].strip()[0] == "=" else 2  # the lines above are its title
        depth, _, consumed = heading_at(lines, j)
        if (depth and consumed == 1) or atx_body(lines[j]) is not None:
            break  # an ATX or HTML heading; a setext one is this paragraph's own lines
        html = html_block(lines[j])
        if (
            THEMATIC_BREAK.match(lines[j])
            or (html and html[1])
            or (
                kind == "paragraph"
                # A quote line ends a paragraph, not the quote it continues.
                and (
                    _INTERRUPTS.match(lines[j])
                    or (not quoted and lines[j].lstrip().startswith(">"))
                )
            )
        ):
            break  # a rule, an HTML block, or a list that may interrupt a paragraph, ends it
        j += 1
    return j, 0


def markdown_blocks(text: str) -> tuple[list[Block], Coverage]:
    src = _Lines(text)
    lines, n = src.lines, len(src.lines)
    acc = Blocks()
    i = _front_matter(src, acc)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = fence_marker(line)
        if fence:
            i = _fence(src, acc, i, fence)
            continue
        depth, title, consumed = heading_at(lines, i)
        if depth and title:
            acc.heading(depth, title, src.loc(i, i + consumed - 1))
            i += consumed
            continue
        body = atx_body(line)
        if body == "":  # an empty ATX heading (`# #`): markup, no title
            src.markup.add(i)
            i += 1
            continue
        if body is not None:  # one whose title cleans to nothing: a one-line paragraph
            acc.add("paragraph", line, src.loc(i, i))
            i += 1
            continue
        if THEMATIC_BREAK.match(line):  # a rule (`- - -` too, not a list item) is markup
            src.markup.add(i)
            i += 1
            continue
        html = html_block(line)
        if html:
            i = _html_lines(src, acc, i, html[0])
            continue
        kind: BlockType = "list" if LIST_ITEM.match(line) else "paragraph"
        j, setext = _paragraph_end(lines, i, kind)
        title = clean_title(" ".join(x.strip() for x in lines[i:j])) if setext else ""
        if title:
            acc.heading(setext, title, src.loc(i, j))
            i = j + 1
            continue
        acc.add(kind, "\n".join(lines[i:j]), src.loc(i, j - 1))
        i = j
    return acc.blocks, src.coverage(acc.blocks)


def paragraph_blocks(text: str) -> tuple[list[Block], Coverage]:
    """Text with no markup: one paragraph per run of non-blank lines."""
    src = _Lines(text)
    acc = Blocks()
    i, n = 0, len(src.lines)
    while i < n:
        if not src.lines[i].strip():
            i += 1
            continue
        j = i
        while j + 1 < n and src.lines[j + 1].strip():
            j += 1
        acc.add("paragraph", "\n".join(src.lines[i : j + 1]), src.loc(i, j))
        i = j + 1
    return acc.blocks, src.coverage(acc.blocks)


_HIDDEN = {"script", "style", "template", "noscript", "head", "svg", "title"}
# What may sit in a <head>; any other start tag ends one left unclosed, as HTML parsing does.
_HEAD_CONTENT = {"title", "meta", "link", "base", "style", "script", "noscript", "template"}
_HEADINGS = {f"h{d}": d for d in range(1, 7)}
_BLOCK_TAGS: dict[str, BlockType] = {
    "p": "paragraph",
    "pre": "code",
    "li": "list",
    "blockquote": "paragraph",
    "td": "paragraph",
    "th": "paragraph",
    "dd": "paragraph",
    "dt": "paragraph",
}
# Elements whose end closes any block left open inside them.
_CONTAINERS = {
    "div",
    "section",
    "article",
    "body",
    "ul",
    "ol",
    "table",
    "tr",
    "dl",
    "main",
    "header",
    "footer",
    "nav",
    "aside",
    "figure",
    "form",
    "details",
    "summary",
    "caption",
}


@dataclass
class _Open:
    tag: str
    line: int
    parts: list[str] = field(default_factory=list)
    first_line: int = 0  # the line its first text starts on
    last_line: int = 0  # the line its last text ends on
    containers: int = 0  # containers opened inside it and not closed yet
    has_paragraphs: bool = False  # its own `<p>`s, for `<li><p>a</p><p>b</p></li>`
    open_paragraphs: int = 0  # those not closed yet

    def take(self, data: str, line: int) -> None:
        """Append `data`, which starts on `line`."""
        self.parts.append(data)
        if data.strip():
            lead = data[: len(data) - len(data.lstrip())].count("\n")
            self.first_line = self.first_line or line + lead
            self.last_line = line + data.rstrip().count("\n")

    def span(self) -> Locator:
        first = self.first_line or self.line
        return Locator(line_start=first, line_end=max(first, self.last_line))


def _visible(text: str) -> int:
    return len("".join(text.split()))


class _HTMLBlocks(HTMLParser):
    """Block elements into blocks. A block closes at its own end tag, at the
    start of the next block, at the end of its container, or at the end of the
    document, so an omitted `</p>`, `</li>` or `</td>` loses no text, and an
    implicitly closed block's span ends on its own last line of text. Every
    visible character outside hidden elements lands in a block; `seen` counts
    them for the coverage record."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.acc = Blocks()
        # Hidden elements open, by tag: counts, not a stack, so nesting costs
        # nothing per tag (an end tag closes the latest of its kind).
        self._hidden: dict[str, int] = {}
        self.hidden_seen = 0  # visible characters inside the hidden elements now open
        self.title = ""  # the text of the page's `<title>`
        self._open: _Open | None = None
        self._loose: _Open | None = None  # text outside any block element
        self.seen = 0

    def _flush_loose(self) -> None:
        if self._loose is not None:
            text = " ".join("".join(self._loose.parts).split())
            if text:
                self.acc.add("paragraph", text, self._loose.span())
            self._loose = None

    def close(self) -> None:
        """The end of the document closes whatever is still open."""
        super().close()
        self._close_open()
        self._flush_loose()

    def _close_open(self) -> None:
        o, self._open = self._open, None
        if o is None:
            return
        raw = "".join(o.parts)
        loc = o.span()
        if o.tag == "pre":
            if raw.strip("\n"):
                self.acc.add("code", raw.strip("\n"), loc)
            return
        text = " ".join(raw.split())
        if not text:
            return
        if o.tag in _HEADINGS:
            self.acc.heading(_HEADINGS[o.tag], text, loc)
        else:
            self.acc.add(_BLOCK_TAGS[o.tag], text, loc)

    def open_hidden(self) -> list[str]:
        return sorted(self._hidden)

    def _unhide(self, tag: str) -> None:
        n = self._hidden.get(tag, 0)
        if n > 1:
            self._hidden[tag] = n - 1
        else:
            self._hidden.pop(tag, None)
        if set(self._hidden) <= {"head"}:
            # Closed: what it held was hidden by design. An unclosed <head> alone
            # holds no text (text ends it), so nothing counted is still unread.
            self.hidden_seen = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._hidden.get("head") and tag not in _HEAD_CONTENT and tag != "head":
            self._unhide("head")  # an unclosed <head> ends at the first body element
        if tag in _HIDDEN:
            self._hidden[tag] = self._hidden.get(tag, 0) + 1
            return
        if self._hidden:
            return
        o = self._open
        if (
            tag == "p"
            and o is not None
            and o.tag == "li"
            and (o.has_paragraphs or not o.first_line)
        ):
            # `<li><p>text</p></li>`, as most site generators write a list: the item's text.
            o.has_paragraphs = True
            o.open_paragraphs += 1
            return
        if tag in _HEADINGS or tag in _BLOCK_TAGS:
            self._close_open()
            self._flush_loose()
            self._open = _Open(tag, self.getpos()[0])
        elif tag in _CONTAINERS:
            # A container starting separates the text around it; inside an open
            # block it is counted, so its end does not close the block.
            self._flush_loose()
            if self._open is not None:
                self._open.containers += 1
                self._open.take("\n", self.getpos()[0])
        elif tag in ("br", "hr"):
            active = self._open or self._loose
            if active is not None:
                active.take("\n", self.getpos()[0])

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN:
            self._unhide(tag)
            return
        if self._hidden:
            return
        if tag == "p" and self._open is not None and self._open.open_paragraphs:
            self._open.open_paragraphs -= 1
            self._open.take("\n", self.getpos()[0])
        elif self._open is not None and tag == self._open.tag:
            self._close_open()
        elif tag in _CONTAINERS and self._open is not None and self._open.containers:
            # A container inside the open block (`<li><div>a</div><div>b</div></li>`)
            # ends; the block goes on.
            self._open.containers -= 1
            self._open.take("\n", self.getpos()[0])
        elif tag in _CONTAINERS:
            self._close_open()
            self._flush_loose()

    def handle_data(self, data: str) -> None:
        if self._hidden == {"head": 1} and data.strip():
            self._hidden, self.hidden_seen = {}, 0  # text ends an unclosed <head>
        if self._hidden:
            if set(self._hidden) <= {"head", "title"} and "title" in self._hidden:
                self.title += data  # the page's name: no body text, but an attr
            self.hidden_seen += _visible(data)
            return
        self.seen += _visible(data)
        line = self.getpos()[0]
        if self._open is not None:
            self._open.take(data, line)
        elif data.strip():
            if self._loose is None:
                self._loose = _Open("", line)
            self._loose.take(data, line)


def _html(text: str) -> tuple[list[Block], Coverage, dict[str, object]]:
    """The page's blocks, their coverage, and its attrs (`title`, the text of its
    `<title>`, which is no block: a page's name, not its body)."""
    p = _HTMLBlocks()
    p.feed(text)
    p.close()
    title = " ".join(p.title.split())
    attrs: dict[str, object] = {"title": title} if title else {}
    # The visible characters the blocks hold, of those outside hidden elements: a
    # guard against a parser change that loses text, not a measure of loss within
    # a block.
    held = sum(_visible(b.text) for b in p.acc.blocks)
    if p.open_hidden() and p.hidden_seen:
        # A hidden element left open hides the rest of the document; a browser
        # would show some of that text, so it is counted as not read.
        tags = ", ".join(f"<{t}>" for t in p.open_hidden())
        return (
            p.acc.blocks,
            Coverage(
                "chars",
                p.seen + p.hidden_seen,
                held,
                failed=(Failure("document", f"{p.hidden_seen} characters inside unclosed {tags}"),),
            ),
            attrs,
        )
    return p.acc.blocks, Coverage("chars", p.seen, held), attrs


def extract_text(text: str, fmt: str) -> Artifact:
    """`fmt` is `markdown`, `html` or `text`. Coverage counts the non-blank
    lines inside a block's span or that are markup (fence and front-matter
    delimiters, thematic breaks, empty ATX headings) for Markdown and text, and
    the visible characters the blocks hold of those outside hidden elements
    for HTML (an unclosed hidden element's text counted as not read): a guard
    against a parser that drops text."""
    attrs: dict[str, object] = {}
    if fmt == "html":
        blocks, coverage, attrs = _html(text)
    else:
        blocks, coverage = (markdown_blocks if fmt == "markdown" else paragraph_blocks)(text)
    return Artifact(
        format=fmt,
        parser=f"loom-{fmt}",
        # HTML is read by the standard library's parser, which changes between Python
        # releases: the version names it, as SQL's names sqlglot's.
        parser_version=on_python(PARSER_VERSION) if fmt == "html" else PARSER_VERSION,
        blocks=tuple(blocks),
        coverage=coverage,
        attrs=attrs,
    )
