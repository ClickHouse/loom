"""Splitting a SQL file into statements: the delimiter mode (`;`, `GO`, `/`,
DELIMITER, psql meta lines, goose blocks), comment handling, hints and each
statement's kind and span."""

from __future__ import annotations

import re
from dataclasses import dataclass

from server.code.domain.extract.lang_sql_model import Hint, Statement, is_header_line


@dataclass(frozen=True)
class SplitMode:
    """How a SQL file's statements end. The default is the `;` at depth 0 the ClickHouse
    corpus uses, with `$tag$ … $tag$` quoting (ClickHouse heredoc strings and PostgreSQL
    function bodies alike). The other modes are switched on by cues in the text: T-SQL
    `GO` batch lines, SQL*Plus `/` lines, the MySQL client's `DELIMITER` command (which
    also makes `#` a line comment), psql's `\\meta` command lines, and the
    comment-delimited blocks migration tools keep whole (`-- +goose StatementBegin …
    StatementEnd`)."""

    dollar_quotes: bool = True
    go_batches: bool = False
    slash_terminator: bool = False
    mysql_delimiter: bool = False
    hash_comments: bool = False
    psql_meta: bool = False
    statement_blocks: tuple[tuple[str, str], ...] = ()


_GO_LINE_RE = re.compile(r"(?im)^[ \t]*GO(?:[ \t]+\d+)?[ \t]*$")
_SLASH_LINE_RE = re.compile(r"(?m)^/[ \t]*$")
_DELIMITER_LINE_RE = re.compile(r"(?im)^[ \t]*DELIMITER[ \t]+(\S+)[ \t]*$")
# psql's own commands (a `\N` in a TSV data line is not one)
_PSQL_META_LINE_RE = re.compile(
    r"(?m)^\\(?:i|ir|include|include_relative|c|connect|set|unset|echo|qecho|warn|o|out|g|gx"
    r"|gset|gexec|gdesc|if|elif|else|endif|pset|timing|x|a|copy|prompt|watch|restrict|unrestrict"
    r"|cd|q|quit|d[a-zA-Z+]*|l\+?|z|sf\+?|sv\+?|ef|ev|e|p|r|w|s|h|help|\?|!|crosstabview|encoding"
    r"|password|conninfo|errverbose|lo_[a-z]+|f|H|t|T|C|bind|getenv|parse|close|startpipeline"
    r"|endpipeline|syncpipeline|flush|flushrequest|getresults|sendpipeline)(?=[\s]|$)"
)
_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$")
GOOSE_STATEMENT_BLOCK = ("-- +goose StatementBegin", "-- +goose StatementEnd")
STATEMENT_BLOCKS: tuple[tuple[str, str], ...] = (GOOSE_STATEMENT_BLOCK,)
DEFAULT_MODE = SplitMode()


def detect_mode(text: str) -> SplitMode:
    """The split mode a file's own text asks for."""
    mysql = bool(_DELIMITER_LINE_RE.search(text))
    blocks = tuple(b for b in STATEMENT_BLOCKS if b[0] in text)
    return SplitMode(
        dollar_quotes=True,
        go_batches=bool(_GO_LINE_RE.search(text)),
        slash_terminator=bool(_SLASH_LINE_RE.search(text)),
        mysql_delimiter=mysql,
        hash_comments=mysql,
        psql_meta=bool(_PSQL_META_LINE_RE.search(text)),
        statement_blocks=blocks,
    )


class _Scanner:
    """Character scanner with the lexical state the splitter needs: string,
    identifier and dollar quoting, `--`, `/* */` (and `#`) comments,
    parenthesis depth, and the current terminator (`;` until a `DELIMITER`
    line says otherwise)."""

    def __init__(self, text: str, mode: SplitMode = DEFAULT_MODE) -> None:
        self.text = text
        self.n = len(text)
        self.i = 0
        self.depth = 0
        self.mode = mode
        self.terminator = ";"

    def _skip_quoted(self, q: str) -> None:
        # ClickHouse strings: '' doubles the quote, \ escapes the next char.
        t, n, i = self.text, self.n, self.i + 1
        while i < n:
            c = t[i]
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == q:
                if i + 1 < n and t[i + 1] == q:
                    i += 2
                    continue
                i += 1
                break
            i += 1
        self.i = i

    def _skip_dollar_quoted(self, tag: str) -> None:
        j = self.text.find(tag, self.i + len(tag))
        self.i = self.n if j < 0 else j + len(tag)

    def _skip_line_comment(self) -> None:
        j = self.text.find("\n", self.i)
        self.i = self.n if j < 0 else j  # leave the newline for the caller

    def _skip_block_comment(self) -> None:
        j = self.text.find("*/", self.i + 2)
        self.i = self.n if j < 0 else j + 2

    def step(self) -> str:
        """Advance over one lexical unit; return ';' when a statement ended
        at depth 0, '' otherwise."""
        t = self.text
        c = t[self.i]
        term = self.terminator
        if term != ";" and t.startswith(term, self.i) and self.depth == 0:
            self.i += len(term)
            return ";"
        if c in "'\"`":
            self._skip_quoted(c)
            return ""
        if c == "$" and self.mode.dollar_quotes:
            m = _DOLLAR_TAG_RE.match(t, self.i)
            if m:
                self._skip_dollar_quoted(m.group(0))
                return ""
        if t.startswith("--", self.i) or (c == "#" and self.mode.hash_comments):
            self._skip_line_comment()
            return ""
        if t.startswith("/*", self.i):
            self._skip_block_comment()
            return ""
        self.i += 1
        if c == "(":
            self.depth += 1
        elif c == ")":
            self.depth = max(0, self.depth - 1)
        elif c == ";" and term == ";" and self.depth == 0:
            return ";"
        return ""


def _line_special(sc: _Scanner, line: str) -> str:
    """What a line that starts at the scanner's position is, in this mode:
    'block' (a kept-whole statement block begins), 'own' (a line that is a
    statement of its own: `DELIMITER x`, a psql `\\meta` command), 'end' (a
    line that terminates the statement before it: `GO`, `/`), or ''."""
    stripped = line.strip()
    mode = sc.mode
    for begin, _ in mode.statement_blocks:
        if stripped.startswith(begin):
            return "block"
    if mode.mysql_delimiter and _DELIMITER_LINE_RE.match(line):
        return "own"
    if mode.psql_meta and _PSQL_META_LINE_RE.match(stripped):
        return "own"
    if mode.go_batches and _GO_LINE_RE.match(line):
        return "end"
    if mode.slash_terminator and _SLASH_LINE_RE.match(line):
        return "end"
    return ""


def _consume_special(
    sc: _Scanner, out: list[tuple[int, int]], start: int, eol: int, kind: str
) -> int:
    """Cut the segments a special line asks for and move the scanner past
    it; returns the new segment start. 'block': the kept-whole block (with
    the directive comments above it) is one segment; 'own': the line is a
    statement of its own (and a `DELIMITER x` line changes the terminator);
    'end': the line terminates the statement before it, `GO`/`/` included."""
    text, n = sc.text, sc.n
    line = text[sc.i : eol]
    if kind == "block":
        _begin, end_marker = next(
            b for b in sc.mode.statement_blocks if line.strip().startswith(b[0])
        )
        j = text.find(end_marker, eol)
        block_eol = text.find("\n", j) if j >= 0 else -1
        block_end = n if j < 0 or block_eol < 0 else block_eol
        out.append((start, block_end))
        sc.i = block_end
        return block_end
    if kind == "own":
        if text[start : sc.i].strip():
            out.append((start, sc.i))
        out.append((sc.i, eol))
        dm = _DELIMITER_LINE_RE.match(line)
        if dm:
            sc.terminator = dm.group(1)
        sc.i = eol
        return eol
    if text[start:eol].strip():  # 'end'
        out.append((start, eol))
    sc.i = eol
    return eol


def _raw_segments(text: str, mode: SplitMode = DEFAULT_MODE) -> list[tuple[int, int]]:
    """(start, end) byte offsets of each raw statement: up to and including
    its terminator, plus the rest of that line when it is blank or a comment
    (the TestHint rides there); the tail after the last terminator when
    non-blank. Line-level terminators (`GO`, `/`), own-line statements
    (`DELIMITER`, psql `\\i`) and kept-whole blocks are cut at their lines."""
    sc = _Scanner(text, mode)
    out: list[tuple[int, int]] = []
    start = 0
    n = sc.n
    special_modes = (
        mode.go_batches
        or mode.slash_terminator
        or mode.mysql_delimiter
        or mode.psql_meta
        or bool(mode.statement_blocks)
    )
    while sc.i < n:
        if special_modes and (sc.i == 0 or text[sc.i - 1] == "\n") and sc.depth == 0:
            eol = text.find("\n", sc.i)
            eol = n if eol < 0 else eol
            kind = _line_special(sc, text[sc.i : eol])
            if kind:
                start = _consume_special(sc, out, start, eol, kind)
                continue
        if sc.step() == ";":
            end = sc.i
            eol = text.find("\n", end)
            eol = n if eol < 0 else eol
            tail = text[end:eol]
            if tail.strip() == "" or tail.lstrip().startswith(("--", "/*")):
                end = eol
            out.append((start, end))
            start = end
    if text[start:].strip():
        out.append((start, n))
    return out


_COMMENT_OR_BLANK = re.compile(r"^\s*(--.*)?$")
_COMMENT_OR_BLANK_HASH = re.compile(r"^\s*(--.*|#.*)?$")


def _split_leading_comments(raw: str, hash_comments: bool = False) -> tuple[str, str]:
    """(leading comment/blank lines, the rest) — the lines before the first
    line that carries a token."""
    lines = raw.split("\n")
    rx = _COMMENT_OR_BLANK_HASH if hash_comments else _COMMENT_OR_BLANK
    signs = ("--", "#") if hash_comments else ("--",)
    k = 0
    while k < len(lines) and rx.match(lines[k]) and lines[k].strip():
        k += 1
    # blank lines at the very top belong to nobody; skip them too
    while k < len(lines) and lines[k].strip() == "":
        k += 1
        if k < len(lines) and lines[k].lstrip().startswith(signs):
            # comment resumes after a blank line: still leading
            while k < len(lines) and rx.match(lines[k]) and lines[k].strip():
                k += 1
    return "\n".join(lines[:k]), "\n".join(lines[k:])


_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_HASH_COMMENT_RE = re.compile(r"(?m)^[ \t]*#[^\n]*")
_OPENER_RE = re.compile(r"--|/\*")
_LINE_OPENER_RE = re.compile(r"--")
_BLOCK_OPENER_RE = re.compile(r"/\*")


def comment_spans(sql: str, line_comments: bool = False) -> list[tuple[int, int]]:
    """The comment spans, left to right, in one pass: each `/* … */`, and each `--` line
    when `line_comments` (whichever starts first wins). A `/*` that never closes is no
    comment, and no later one closes either, so it is not searched to the end from each."""
    opener = _OPENER_RE if line_comments else _BLOCK_OPENER_RE
    spans: list[tuple[int, int]] = []
    pos = 0
    while m := opener.search(sql, pos):
        start = m.start()
        if m.group() == "--":
            end = sql.find("\n", start)
            end = len(sql) if end < 0 else end
        elif (end := sql.find("*/", start + 2)) >= 0:
            end += 2
        elif line_comments:
            opener, pos = _LINE_OPENER_RE, start + 1
            continue
        else:
            break
        spans.append((start, end))
        pos = end
    return spans


def without_spans(text: str, spans: list[tuple[int, int]], fill: str = "") -> str:
    """`text` with each span replaced by `fill`."""
    out: list[str] = []
    last = 0
    for start, end in spans:
        out += (text[last:start], fill)
        last = end
    out.append(text[last:])
    return "".join(out)


def strip_comments(sql: str, hash_comments: bool = False) -> str:
    """The statement without its comments (strings are not inspected: a
    `--` inside a literal is rare in the tree and only costs a facet)."""
    out = _LINE_COMMENT_RE.sub("", without_spans(sql, comment_spans(sql)))
    return _HASH_COMMENT_RE.sub("", out) if hash_comments else out


# --------------------------------------------------------------------------
# TestHint
# --------------------------------------------------------------------------

_HINT_WORD_RE = re.compile(r"\{([^{}]*)\}")
_HINT_ERROR_KINDS = ("serverError", "clientError", "error")
_HINT_ECHO = ("echoOn", "echoOff", "echo")


def parse_hint(comments: list[str]) -> Hint:
    """TestHint over the comments that follow a statement's first token:
    the text between the first `{` and the next `}` of each, lexed as
    words; `echo|echoOn|echoOff`, `retry N until|while`, and an error kind
    followed by a comma list of numbers or bare names."""
    errors: list[str] = []
    echo = retry = ""
    for c in comments:
        m = _HINT_WORD_RE.search(c)
        if not m:
            continue
        words = m.group(1).replace(",", " , ").split()
        i = 0
        while i < len(words):
            w = words[i]
            if w in _HINT_ECHO:
                echo = w
            elif w == "retry" and i + 2 < len(words) and words[i + 2] in ("until", "while"):
                retry = f"retry {words[i + 1]} {words[i + 2]}"
                i += 2
            elif w in _HINT_ERROR_KINDS:
                i += 1
                while i < len(words) and (words[i] == "," or re.fullmatch(r"\w+", words[i])):
                    if words[i] != ",":
                        errors.append(words[i])
                    i += 1
                continue
            i += 1
    return Hint(errors=tuple(errors), echo=echo, retry=retry)


def _trailing_comments(rest: str) -> list[str]:
    """Comments that follow the statement's first token (leading ones are
    ignored by the client, so they are ignored here)."""
    out = [rest[start:end] for start, end in comment_spans(rest)]
    out.extend(_LINE_COMMENT_RE.findall(rest))
    return out


# --------------------------------------------------------------------------
# kind
# --------------------------------------------------------------------------

_KIND_SKIP = frozenset({"OR", "REPLACE", "IF", "NOT", "EXISTS", "TEMPORARY"})
_TWO_WORD_HEADS = frozenset(
    {
        "CREATE",
        "DROP",
        "ALTER",
        "SYSTEM",
        "SHOW",
        "ATTACH",
        "DETACH",
        "RENAME",
        "TRUNCATE",
        "KILL",
        "GRANT",
        "REVOKE",
        "OPTIMIZE",
    }
)
_WORD_RE = re.compile(r"[A-Za-z_]\w*")


_PSQL_META_KIND_RE = re.compile(r"^\\([a-zA-Z!?]+)")


def statement_kind(body: str) -> str:
    """`SELECT`, `CREATE TABLE`, `SYSTEM FLUSH`, … from the statement's first
    one or two keywords; `WITH … SELECT` is a SELECT; `(SELECT …)` too."""
    meta = _PSQL_META_KIND_RE.match(body.lstrip())
    if meta:
        return "\\" + meta.group(1)  # a psql meta-command: `\i`, `\set`, `\c`
    words: list[str] = [str(w).upper() for w in _WORD_RE.findall(body)[:8]]
    if not words:
        return "EMPTY"
    head = words[0]
    if head == "WITH":
        return "SELECT"
    if head not in _TWO_WORD_HEADS:
        return head
    for w in words[1:]:
        if w not in _KIND_SKIP:
            return f"{head} {w}"
    return head


# --------------------------------------------------------------------------
# statements
# --------------------------------------------------------------------------


def split_statements(text: str, mode: SplitMode | None = None) -> list[Statement]:
    """The file's statement records in order. Header lines (`-- Tags:` and
    the runner's other keys) are not statements and are dropped from the
    leading comments of whichever statement they precede. `mode` defaults
    to what the text itself asks for (`detect_mode`)."""
    out: list[Statement] = []
    mode = detect_mode(text) if mode is None else mode
    # Line numbers and comment-only tails are carried forward, not recounted from the
    # top or re-copied per segment, which costs quadratic time in a file of many.
    pos, line = 0, 1
    tail: list[str] = []
    for start, end in _raw_segments(text, mode):
        raw = text[start:end]
        leading, rest = _split_leading_comments(raw, mode.hash_comments)
        if not rest.strip() and not leading.strip():
            continue
        if not rest.strip():
            # a trailing comment-only tail (a stray hint, a closing remark):
            # it belongs to the previous statement's text, never a record
            if out:
                tail.append(raw)
            continue
        _join_tail(out, tail)
        line, pos = line + text.count("\n", pos, start), start
        comment_lines = [ln for ln in leading.split("\n") if ln.strip() and not is_header_line(ln)]
        body = strip_comments(rest, mode.hash_comments)
        first_line, last_line = _span_of(line, raw)
        stmt = Statement(
            ordinal=len(out) + 1,
            start_line=first_line,
            end_line=last_line,
            text=raw,
            body=body.strip(),
            kind=statement_kind(body),
            hint=parse_hint(_trailing_comments(rest)),
            comments="\n".join(comment_lines),
        )
        out.append(stmt)
    _join_tail(out, tail)
    return out


def _join_tail(out: list[Statement], tail: list[str]) -> None:
    """The comment-only segments after the last statement, added to its text."""
    if tail:
        out[-1].text += "".join(tail)
        # `start_line` is past the blank lines `text` starts with; as in `_span_of`, the
        # lines of only whitespace at its end are nobody's
        last = out[-1]
        last.end_line = last.start_line - _blank_head(last.text) + last.text.rstrip().count("\n")
        tail.clear()


def _blank_head(raw: str) -> int:
    """Leading lines of a raw segment that belong to no statement: blank
    lines (the newline that closed the previous statement) and the runner's
    header lines (`-- Tags:` …), which the file-header chunk carries."""
    n = 0
    for ln in raw.split("\n"):
        if ln.strip() and not is_header_line(ln):
            break
        n += 1
    return n


def _span_of(start_line: int, raw: str) -> tuple[int, int]:
    """(first, last) file lines of a raw segment's kept lines, from the line it starts
    on: the blank lines at its top (the newline that closed the previous statement)
    belong to nobody, and so does a trailing empty split."""
    lines = raw.split("\n")
    head = _blank_head(raw)
    kept = lines[head:]
    while kept and kept[-1].strip() == "":
        kept.pop()
    first = start_line + head
    return first, first + max(0, len(kept) - 1)
