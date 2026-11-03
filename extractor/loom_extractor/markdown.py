"""Markdown line rules the text parsers read headings, fences, rules and HTML
blocks by, after CommonMark: an ATX or setext heading and its cleaned title, a
fenced code block's opener and closer, a list item, a thematic break, an HTML
block's start and end.

Pure and I/O-free.
"""

from __future__ import annotations

import re

# The closing `#` run is stripped in `_atx` (a pattern tail backtracks on long
# blanks); a bare `#` is an empty heading, `#hashtag` none.
_ATX = re.compile(r"^(#{1,6})(?:[ \t]+(.*)|[ \t]*\r?)$")
# Indented up to three spaces (a tab is four columns); `\r?`: a CRLF line keeps its `\r`.
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*\r?$")
_HTML_H = re.compile(r"^\s*<h([1-6])[^>]*>(.*?)</h\1>\s*$", re.IGNORECASE)
_FENCE_RUN = re.compile(r"^\s*(`{3,}|~{3,})")


def fence_marker(line: str) -> str:
    """The fence `line` opens (its run of backticks or tildes), or ''. A backtick
    fence's info string holds no backtick: "```js``` is inline" opens nothing.
    Checked after the match, not by a lookahead the run would backtrack into."""
    m = _FENCE_RUN.match(line)
    if m is None or (m.group(1)[0] == "`" and "`" in line[m.end() :]):
        return ""
    return m.group(1)


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
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])(?:\s+|$)")
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
# marker, 6 (a block-level tag) and 7 (a line of only tags) at a blank line.
# A tag's attributes are one `[^>]*` run, so a repeated tag has one way to match
# and the pattern stays linear.
_HTML_OPEN = re.compile(
    r"^ {0,3}(?:<(?P<raw>pre|script|style|textarea)(?=[\s>]|$)|(?P<comment><!--)"
    r"|(?P<pi><\?)|(?P<cdata><!\[CDATA\[)|(?P<decl><![A-Za-z])"
    rf"|(?P<block></?(?:{_BLOCK_TAGS})(?=[\s/>]|$))"
    r"|(?P<tags>(?:</?[A-Za-z][\w-]*(?:[\s/][^>]*)?>\s*)+$))",
    re.IGNORECASE,
)
_HTML_END = {"comment": "-->", "pi": "?>", "cdata": "]]>", "decl": ">"}


def html_block(line: str) -> tuple[str, bool] | None:
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
THEMATIC_BREAK = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*\r?$")
# A matched `*`/`**` pair: a lone `*` (`2 * 3`, `*args`) stays, and underscores
# are left alone, since `__init__` and `max_rows` are names far more often than emphasis.
_EMPHASIS = re.compile(r"(?<![\w*])(\*{1,3})(?=\S)([^*]+?)(?<=\S)\1(?![\w*])")
# Characters of a heading line its markup is read in (the rest is kept as written),
# so the title patterns above stay bounded.
TITLE_MAX = 500  # not a setting: a bound on hostile input, not a tuning knob


# These heading, fence and title helpers build Markdown artifacts: a change to
# what they return bumps `text.py`'s PARSER_VERSION.


def clean_title(raw: str) -> str:
    """A heading's text without its markup: inline HTML tags (not other `<...>`
    text), code backticks (not the code), and matched `*` emphasis outside code
    (not a lone `*`, and never an underscore), and a trailing heading-id
    attribute (`{#anchor}`). The markup is read in the first `TITLE_MAX`
    characters; the rest is kept as written."""
    text = _without_anchor(raw.strip())
    head, rest = text[:TITLE_MAX], text[TITLE_MAX:]
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


def _atx(body: str) -> str:
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


def closes_fence(line: str, opener: str) -> bool:
    """Whether `line` closes the fence `opener` opened: a run of the same
    character at least as long, and nothing else on the line."""
    run = line.strip()
    return len(run) >= len(opener) and set(run) == {opener[0]}


def _atx_line(line: str) -> tuple[int, str] | None:
    """(depth, text before cleanup) of an ATX heading line, the text `''` for an
    empty heading such as `# #`; None when `line` is no ATX heading."""
    m = _ATX.match(line)
    return (len(m.group(1)), _atx(m.group(2) or "")) if m else None


def atx_body(line: str) -> str | None:
    """An ATX heading line's text before cleanup (`''` for an empty heading such
    as `# #`), or None when `line` is no ATX heading."""
    atx = _atx_line(line)
    return atx[1] if atx else None


def heading_at(lines: list[str], i: int) -> tuple[int, str, int]:
    """(depth, title, lines consumed) of a heading starting at line `i`: ATX,
    HTML, or setext (the text line plus its underline); depth 0 when the line
    starts none."""
    line = lines[i]
    atx = _atx_line(line)
    if atx is not None:  # an ATX line, even an empty one (`# #`), is never a setext title
        depth, body = atx
        return (depth, clean_title(body), 1) if body else (0, "", 1)
    h = _HTML_H.match(line)
    if h:
        return int(h.group(1)), clean_title(h.group(2)), 1
    # The underline first, which almost no line has; then a list item, a quote, a
    # rule or an HTML block's line over it is that, not a heading's title.
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    if (
        SETEXT_UNDERLINE.match(nxt)
        and len(nxt.strip()) >= 3
        and line.strip()
        and not LIST_ITEM.match(line)
        and not THEMATIC_BREAK.match(line)
        and html_block(line) is None
        # A line closing a comment: read one line at a time, it may be inside one.
        and not line.rstrip().endswith("-->")
        and not line.lstrip().startswith(">")
    ):
        return (1 if nxt.strip()[0] == "=" else 2), clean_title(line), 2
    return 0, "", 1
