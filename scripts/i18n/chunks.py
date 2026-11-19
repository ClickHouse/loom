"""Shared Markdown tokens and byte-preserving bounded translation requests."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from itertools import accumulate, pairwise
from xml.etree.ElementTree import Element

from markdown import Markdown
from markdown.treeprocessors import Treeprocessor
from markdown_it import MarkdownIt
from markdown_it.token import Token

from scripts.i18n.context import Label, SourceContext

FENCE = re.compile(r"^(`{3,}|~{3,})")
# A literal comparison is prose; quoted delimiters and comments belong to their tag.
HTML_TAG = re.compile(
    r"<!--.*?-->|<\?.*?\?>|<!\[CDATA\[.*?\]\]>"
    r"|<(?:/?[A-Za-z][^\s/<>\"']*(?=[\s/>])|![A-Za-z])"
    r"(?:[^<>\"']|\"[^\"]*\"|'[^']*')*>",
    re.DOTALL,
)

CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)

ANCHOR = re.compile(r'^<a id="([^"]+)"></a>$')


def outside_fences(text: str) -> list[tuple[str, bool]]:
    """The page's lines, each with whether it is inside a fenced code block."""
    lines, fence = [], None
    for line in text.split("\n"):
        mark = FENCE.match(line.lstrip())
        if fence is None and mark:
            fence = mark.group(1)
            lines.append((line, True))
        elif fence is not None:
            lines.append((line, True))
            if line.strip().startswith(fence) and line.strip().strip(fence[0]) == "":
                fence = None
        else:
            lines.append((line, False))
    return lines


MAX_BYTES = 16_384
_TAG_NAME = re.compile(r"</?([A-Za-z][^\s/<>\"']*)")
_BLOCK = frozenset(
    "address article aside blockquote details dialog div dl fieldset figure footer form "
    "h1 h2 h3 h4 h5 h6 header hgroup hr iframe main menu nav ol p pre script section style "
    "summary table tbody td tfoot th thead tr ul".split()
)
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


def _code_blocks(text: str) -> list[tuple[int, int]]:
    lines, starts, spans = text.splitlines(), _line_starts(text), []
    for token in MarkdownIt("commonmark").parse(text):
        if token.type not in {"fence", "code_block"} or token.map is None:
            continue
        first, end = token.map
        if token.type == "fence":
            closing = _site_line(lines[end - 1])[1].strip()
            if end <= first + 1 or not re.fullmatch(
                re.escape(token.markup[0]) + "{" + str(len(token.markup)) + ",}", closing
            ):
                raise ValueError("cannot split an unclosed fenced code block")
        spans.append((starts[first], starts[end]))
    return spans


def _html(text: str) -> list[tuple[int, int]]:
    spans, stack = [], []
    matches = list(HTML_TAG.finditer(text))
    closing = {_TAG_NAME.match(m[0])[1].lower() for m in matches if m[0].startswith("</")}
    for match in matches:
        raw = match[0]
        if raw.startswith(("<!--", "<?", "<!")):
            spans.append(match.span())
            continue
        name = _TAG_NAME.match(raw)[1].lower()
        if raw.startswith("</"):
            if not stack or stack[-1][0] != name:
                raise ValueError(f"cannot split misnested HTML: {name}")
            _, start = stack.pop()
            spans.append((start, match.end()))
        elif name not in _VOID and not raw.endswith("/>") and name in closing:
            stack.append((name, match.start()))
        elif (
            name in _BLOCK
            and name not in _VOID
            and not raw.endswith("/>")
            and not text[text.rfind("\n", 0, match.start()) + 1 : match.start()].strip()
        ):
            raise ValueError(f"cannot split unclosed HTML: {name}")
        else:
            spans.append(match.span())
    if stack:
        raise ValueError("cannot split an unclosed HTML element")
    return spans


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _lists(text: str) -> list[tuple[int, int]]:
    starts = _line_starts(text)
    return [
        (starts[token.map[0]], starts[token.map[1]])
        for token in MarkdownIt("commonmark").enable("table").parse(text)
        if token.type == "list_item_open" and token.map is not None
    ]


def _site_line(line: str) -> tuple[int, str]:
    depth, logical = 0, line.expandtabs(4)
    while match := re.match(r"^ *> ?", logical):
        depth += 1
        logical = logical[match.end() :]
    return depth, logical


def _site_lists(text: str, protected: list[tuple[int, int]]) -> list[tuple[int, int]]:
    # The site's two-space list extension has no source maps; protect its continuations
    # in addition to CommonMark's marker-width-dependent ranges.
    ordered = sorted(protected)
    starts = [a for a, _ in ordered]
    ends = list(accumulate((b for _, b in ordered), max))
    exact_starts = set(starts)
    spans = []
    active, after_blank, offset = None, False, 0
    for line in text.splitlines(keepends=True):
        index = bisect_right(starts, offset)
        inside = bool(index and offset < ends[index - 1])
        depth, logical = _site_line(line)
        item = None if inside else re.match(r"^( *)(?:[-+*]|\d+[.)])\s", logical)
        indent = len(logical) - len(logical.lstrip(" "))
        new_block = not inside or offset in exact_starts
        if (
            active is not None
            and new_block
            and (depth != active[2] or (after_blank and logical.strip() and indent < active[1] + 2))
        ):
            spans.append((active[0], offset))
            active, after_blank = None, False
        if item and (active is None or len(item[1]) <= active[1]):
            if active is not None:
                spans.append((active[0], offset))
            active, after_blank = (offset, len(item[1]), depth), False
        elif active is not None and not inside:
            after_blank = not logical.strip()
        offset += len(line)
    if active is not None:
        spans.append((active[0], len(text)))
    return spans


def _protected(text: str) -> list[tuple[int, int]]:
    spans = _code_blocks(text)
    masked = list(text)
    for a, b in spans:
        masked[a:b] = ["\n" if c == "\n" else " " for c in text[a:b]]
    for match in CODE_SPAN.finditer("".join(masked)):
        spans.append(match.span())
        masked[match.start() : match.end()] = ["\n" if c == "\n" else " " for c in match[0]]
    spans.extend(_html("".join(masked)))
    return spans + _lists(text) + _site_lists(text, spans)


def heading_sections(text: str) -> list[str]:
    """Cut before anchored second-level headings outside complete Markdown units."""
    protected = _protected(text)
    cuts = [0]
    for match in re.finditer(r'^<a id="[^"]+"></a>\n\n(?=## )', text, re.MULTILINE):
        offset = match.start()
        if offset and not any(a < offset < b for a, b in protected):
            cuts.append(offset)
    cuts.append(len(text))
    parts = [text[a:b] for a, b in pairwise(cuts)]
    return [part.removesuffix("\n") for part in parts[:-1]] + parts[-1:]


def _table_rows(text: str) -> dict[int, str]:
    """Source row offsets from the parser; a header and delimiter stay together."""
    starts = _line_starts(text)
    rows = {}
    for token in MarkdownIt("commonmark").enable("table").parse(text):
        if token.map is None:
            continue
        if token.type == "table_open":
            rows[starts[token.map[0] + 1]] = "delimiter"
        elif token.type == "tr_open":
            rows[starts[token.map[0]]] = "row"
    return rows


def _boundaries(text: str) -> list[int]:
    lines, offset, last = text.splitlines(keepends=True), 0, ""
    protected = _protected(text)
    rows = _table_rows(text)
    items = {a for a, _ in [*_lists(text), *_site_lists(text, [])]}
    boundaries = [0]
    for previous, _current in pairwise(lines):
        offset += len(previous)
        if previous.strip():
            last = previous.strip()
        blank = not previous.strip() and not ANCHOR.fullmatch(last)
        row = offset in rows and offset - len(previous) in rows and rows[offset] != "delimiter"
        if (blank or row or offset in items) and not any(a < offset < b for a, b in protected):
            boundaries.append(offset)
    return [*boundaries, len(text)]


def request_boundaries(text: str) -> tuple[int, ...]:
    """Safe cuts in a complete prepared container, before separator removal."""
    return tuple(_boundaries(text))


def _context_boundaries(text: str, scope: SourceContext) -> list[int]:
    if (
        not 0 <= scope.start <= scope.end <= len(scope.english)
        or scope.english[scope.start : scope.end] != text
    ):
        raise ValueError("translation range does not match its source context")
    boundaries = _boundaries(scope.english)
    # An assembly separator can own the newline immediately before a safe cut.
    safe_end = scope.end in boundaries or (
        scope.end < len(scope.english)
        and scope.english[scope.end] == "\n"
        and scope.end + 1 in boundaries
    )
    if scope.start not in boundaries or not safe_end:
        raise ValueError("translation range endpoints are not parser-safe in its source context")
    return [
        0,
        *(cut - scope.start for cut in boundaries if scope.start < cut < scope.end),
        len(text),
    ]


def validate_request_bound(bound: int) -> None:
    if type(bound) is not int or not 1 <= bound <= MAX_BYTES:
        raise ValueError(f"translation request byte bound must be an integer from 1 to {MAX_BYTES}")


def split(
    text: str,
    *,
    source_context: SourceContext | None = None,
    bounded_new_request_bytes: int = MAX_BYTES,
) -> list[str]:
    """Keep complete Markdown units within one bounded request.

    A headerless slice uses its complete source context's table and container boundaries.
    """
    validate_request_bound(bounded_new_request_bytes)
    boundaries = _context_boundaries(text, source_context) if source_context else None
    if len(text.encode()) <= bounded_new_request_bytes:
        return [text]
    if boundaries is None:
        boundaries = _boundaries(text)
    for a, b in pairwise(boundaries):
        if len(text[a:b].encode()) > bounded_new_request_bytes:
            raise ValueError(
                "indivisible Markdown unit exceeds the "
                f"{bounded_new_request_bytes}-byte translation request bound"
            )
    cuts, previous = [0], 0
    for end in boundaries[1:]:
        if len(text[cuts[-1] : end].encode()) > bounded_new_request_bytes:
            cuts.append(previous)
        previous = end
    cuts.append(len(text))
    parts = [text[a:b] for a, b in pairwise(cuts)]
    # The caller joins requests with one newline, as it does between heading sections.
    return [part.removesuffix("\n") for part in parts[:-1]] + parts[-1:]


def _layout(element: Element, cell_text: dict[Element, bool]) -> object:
    tags = {"thead", "tbody", "tr", "th", "td"}
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        cell_text.get(element),
        tuple(_layout(child, cell_text) for child in element if child.tag in tags),
    )


def _tables(
    element: Element, cell_text: dict[Element, bool], parents: tuple[object, ...] = ()
) -> list[object]:
    if element.tag == "table":
        return [(parents, _layout(element, cell_text))]
    context = (*parents, (element.tag, tuple(sorted(element.attrib.items()))))
    return [table for child in element for table in _tables(child, cell_text, context)]


class _CellText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.prose: list[str] = []
        self.code: list[str] = []
        self.in_code = False
        self.links: list[tuple[str, str]] = []
        self.link: tuple[int, str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "code":
            self.in_code = True
            self.code.append("")
            self.prose.append(" ")
        elif tag == "a":
            self.link = (len(self.prose), dict(attrs).get("href", ""))

    def handle_endtag(self, tag):
        if tag == "code":
            self.in_code = False
        elif tag == "a" and self.link is not None:
            start, target = self.link
            self.links.append(("".join(self.prose[start:]), target))
            self.link = None

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self.in_code:
            self.code[-1] += data
        else:
            self.prose.append(data)

    def label(self, start: int, end: int) -> Label:
        return Label(
            " ".join("".join(self.prose).split()),
            minimum=2,
            links=tuple(self.links),
            code=tuple(self.code),
            start=start,
            end=end,
        )


class _TableShape(Treeprocessor):
    def __init__(self, md: Markdown) -> None:
        super().__init__(md)
        self.tables: list[object] = []
        self.rows: list[list[Label]] = []

    def run(self, root: Element) -> None:
        cell_text = {}
        labels = {}
        for cell in root.iter():
            if cell.tag not in {"th", "td"}:
                continue
            # Resolve raw HTML stashes after escaping ordinary text through the serializer.
            html = self.md.serializer(cell)
            for name in ("raw_html", "amp_substitute"):
                html = self.md.postprocessors[name].run(html)
            parser = _CellText()
            parser.feed(html)
            cell_text[cell] = bool("".join(parser.text).strip())
            labels[cell] = parser.label(0, 0)
        self.tables = _tables(root, cell_text)
        self.rows = [[labels[cell] for cell in row if cell in labels] for row in root.iter("tr")]


def _github_parse(text: str) -> tuple[MarkdownIt, list[Token]]:
    md = MarkdownIt("commonmark").enable("table")
    return md, md.parse(text)


def _github_tables(text: str, *, tokens: list[Token] | None = None) -> list[object]:
    stack, tables = [], []
    inside = False
    for token in _github_parse(text)[1] if tokens is None else tokens:
        attributes = tuple(sorted(token.attrs.items()))
        if token.type == "table_open":
            tables.append(tuple(stack))
            inside = True
        if inside and token.nesting:
            tables.append((token.type, attributes))
        elif inside and token.type == "inline":
            # Ignore translated words while retaining whether a cell contains text.
            content = "".join(
                child.content
                for child in token.children or []
                if child.type in {"text", "code_inline"}
            )
            tables.append(("cell_text", bool(content.strip())))
        if token.type == "table_close":
            inside = False
        if token.nesting == 1:
            stack.append((token.type, attributes))
        elif token.nesting == -1:
            stack.pop()
    return tables


def _site_tables(text: str) -> _TableShape:
    md = Markdown(
        extensions=[
            "tables",
            "admonition",
            "attr_list",
            "mdx_truly_sane_lists",
            "pymdownx.tasklist",
            "pymdownx.superfences",
        ]
    )
    shape = _TableShape(md)
    md.treeprocessors.register(shape, "translation_table_shape", 0)
    md.convert(text)
    return shape


def table_shape(text: str) -> tuple[list[object], list[object]]:
    """Rendered table structure, including containers, rows, cells and alignment."""
    shape = _site_tables(text)
    return shape.tables, _github_tables(text)


def _github_rows(
    text: str, *, parsed: tuple[MarkdownIt, list[Token]] | None = None
) -> list[list[Label]]:
    md, tokens = _github_parse(text) if parsed is None else parsed
    starts, rows, row = _line_starts(text), [], None
    first = end = 0
    for token in tokens:
        if token.type == "tr_open" and token.map is not None:
            row = []
            first, end = (starts[n] for n in token.map)
            # A request cut removes its separator newline, not the row's text.
            end = first + len(text[first:end].rstrip("\r\n"))
        elif token.type == "inline" and row is not None:
            parser = _CellText()
            parser.feed(md.renderer.renderInline(token.children or [], md.options, {}))
            row.append(parser.label(first, end))
        elif token.type == "tr_close" and row is not None:
            rows.append(row)
            row = None
    return rows


def table_labels(text: str) -> dict[tuple[str, int, int], Label]:
    """Cell text from both existing dialects; source maps survive inline-code pipes."""
    return _table_labels(text, _github_rows(text), _site_tables(text).rows)


def _table_labels(
    text: str, github: list[list[Label]], site: list[list[Label]]
) -> dict[tuple[str, int, int], Label]:
    by_label: dict[tuple[Label, ...], deque[int]] = defaultdict(deque)
    for index, row in enumerate(github):
        by_label[tuple(replace(c, start=0, end=0) for c in row)].append(index)
    consumed = set()
    rows = []
    for row in site:
        matches = by_label.get(tuple(row))
        if matches:
            index = matches.popleft()
            consumed.add(index)
            rows.append(github[index])
        else:
            # The site parser has no source maps. These obligations remain page-wide.
            rows.append([replace(c, end=len(text)) for c in row])
    rows.extend(row for index, row in enumerate(github) if index not in consumed)
    labels = {}
    for index, row in enumerate(rows):
        field = row[0].code[0] if row and not row[0].text and len(row[0].code) == 1 else ""
        fields = row[0].code if row and not row[0].text.strip(", ") else ()
        kind = row[1].code if len(row) > 1 else ()
        for column, cell in enumerate(row):
            labels[("cell", index, column)] = replace(
                cell,
                field=field if column == len(row) - 1 else "",
                string_field=bool(field and column == 2 and kind == ("str",)),
                default_for=field if column == 1 and len(row) == 3 else "",
                family_for=fields if column == 1 and len(row) == 3 else (),
            )
    return labels


@dataclass(frozen=True)
class TableAnalysis:
    labels: dict[tuple[str, int, int], Label]
    shape: tuple[list[object], list[object]]


def table_analysis(text: str) -> TableAnalysis:
    """Derive labels and structure from the same parse of each dialect."""
    parsed = _github_parse(text)
    site = _site_tables(text)
    return TableAnalysis(
        _table_labels(text, _github_rows(text, parsed=parsed), site.rows),
        (site.tables, _github_tables(text, tokens=parsed[1])),
    )
