"""SQL scripts into one block per statement, parsed and never executed.

Statements are split on the dialect's own tokens, so a `;` inside a string or
a comment does not end one. Each statement keeps its line span, its comments,
what it is (`CREATE TABLE`, `SELECT`, ...), the object it creates or writes and
the tables it reads (a CTE is not a table); the names go into the block's
`keys` so an exact lookup finds the statement that defines `events`. A comment
on the line of a `;`, after it, belongs to the statement that `;` closes.

The dialect is the caller's, or detected: the candidates whose tokenizers read
the longest window of the script rank first, and of those the one that turns
the largest share of the first statements' text into real syntax trees wins; a
tie between candidates that parse any is reported (`attrs.dialect_candidates`).
A statement the chosen dialect cannot turn into a syntax tree, or a script the
tokenizer cannot split, still becomes searchable text, and its loss of structure
is recorded in coverage (`sql_structure`), never hidden.

Pure; needs `sqlglot`. The extraction child quiets sqlglot's per-statement
warnings, which coverage records.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from loom_extractor.artifact import (
    Artifact,
    Block,
    Coverage,
    Failure,
    Lines,
    Locator,
)

if TYPE_CHECKING:
    from sqlglot import exp

# Bumped when this module's output changes for the same input and sqlglot version.
PARSER_VERSION = "1"

# Tried in this order when no dialect is declared; the first of a tie is chosen.
DETECT_DIALECTS = (
    "clickhouse",
    "postgres",
    "mysql",
    "bigquery",
    "snowflake",
    "duckdb",
    "sqlite",
    "tsql",
)
# Detection reads the whole script when it is at most DETECT_CHARS, else prefixes
# ending at one of its first DETECT_STATEMENTS `;`; it scores at most that many statements.
DETECT_STATEMENTS = 50
DETECT_CHARS = 50_000


class UnknownDialect(ValueError):
    """The declared dialect is not one the parser knows."""


@dataclass(frozen=True)
class _Span:
    start: int  # char offsets into the script, exclusive end
    end: int
    comments: tuple[str, ...]
    identifiers: tuple[str, ...]
    has_tokens: bool  # False for a span of comments alone
    code_end: int  # where the statement's own text ends: its `;`, before a comment after it


def _spans(text: str, dialect: str) -> list[_Span]:
    """Statement spans by the dialect's tokens: each runs from where the previous
    one ended to its own `;` (or the end of the script), trimmed of whitespace,
    so a statement's leading comment belongs to it. sqlglot hangs a comment on
    the nearest token; one it hangs on a `;` belongs to the statement that `;`
    closes when it sits before the `;` or on its line (the span then runs to the
    end of that line), and to the next statement when it sits on a later line."""
    import sqlglot

    # sqlglot.tokens re-exports it without declaring the export, which mypy --strict refuses.
    from sqlglot.tokenizer_core import TokenType

    tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(text)
    spans: list[_Span] = []
    begin = 0
    comments: list[str] = []
    idents: list[str] = []
    has_tokens = False

    def close(end: int, code_end: int) -> None:
        raw = text[begin:end]
        lead = len(raw) - len(raw.lstrip())
        if raw.strip() not in ("", ";"):  # an empty statement (`;;`) is none
            spans.append(
                _Span(
                    begin + lead,
                    begin + len(raw.rstrip()),
                    tuple(comments),
                    tuple(idents),
                    has_tokens,
                    min(code_end, begin + len(raw.rstrip())),
                )
            )

    for i, tok in enumerate(tokens):
        said = [c.strip() for c in tok.comments if c.strip()]
        if tok.token_type != TokenType.SEMICOLON:
            comments.extend(said)
            has_tokens = True
            if tok.token_type in (TokenType.VAR, TokenType.IDENTIFIER):
                idents.append(tok.text)
            continue
        end = code_end = tok.end + 1
        # The tokenizer hangs a comment before a `;` on the token before it, so
        # one on the `;` follows it: on its line it is the closing statement's,
        # unless another statement starts on that line too.
        next_start = tokens[i + 1].start if i + 1 < len(tokens) else len(text)
        nl, same_line = -1, False
        if said:  # the `;` line's end, looked for no further than the next token
            nl = text.find("\n", end, next_start)
            # No newline before the next token: the next statement starts on this
            # line, and the comments are its.
            same_line = nl < 0 and i + 1 < len(tokens)
        later: list[str] = []
        search = code_end  # past each match, so one comment is not found inside another
        for c in said:
            at = -1 if same_line else text.find(c, search, next_start)
            if at >= 0 and (nl < 0 or at < nl):  # it starts on the `;` line
                comments.append(c)
                search = _comment_end(text, at, at + len(c), next_start)
                # The span runs to the end of the line the comment ends on, and a
                # comment starting on that line is this statement's too.
                after = text.find("\n", search, next_start)
                end = after if after >= 0 else (search if i + 1 < len(tokens) else next_start)
                nl = after if after >= 0 else nl
            elif not has_tokens and text.find(c, begin, tok.start) >= 0:
                comments.append(c)  # a span of comments alone, closed by a bare `;`
            else:
                later.append(c)
        close(end, code_end)
        begin, comments, idents, has_tokens = end, later, [], False
    close(len(text), len(text))
    return spans


def _comment_end(text: str, start: int, stop: int, limit: int) -> int:
    """Where the comment whose stripped text is `text[start:stop]` ends: past its
    `*/` when its opener before the text is `/*`, else at its line's end (a
    `--` or `#` comment), looking no further than `limit`."""
    j = start
    while j > 0 and text[j - 1] in " \t\r\n":
        j -= 1
    if text[max(0, j - 2) : j] == "/*":
        close = text.find("*/", stop, limit)
        return close + 2 if close >= 0 else limit
    newline = text.find("\n", stop, limit)
    return newline if newline >= 0 else limit


def _parse(sql: str, dialect: str) -> exp.Expression | None:
    """The statement's syntax tree, or None when the dialect cannot parse it
    into real syntax (an error, or sqlglot's opaque `Command` fallback)."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql.rstrip().rstrip(";"), read=dialect)
    except Exception:  # noqa: BLE001 — a syntax error, deep nesting or a parser bug on hostile input is an unparsed statement, not a failed file
        return None
    # A Block is several statements sqlglot found in one span (a body the client
    # keeps whole): not one statement's tree.
    if not isinstance(tree, exp.Expression) or isinstance(tree, (exp.Command, exp.Block)):
        return None
    return tree


@dataclass(frozen=True)
class _Described:
    kind: str  # CREATE TABLE, CREATE MATERIALIZED VIEW, SELECT, ...
    defines: str  # the object a CREATE creates
    writes: tuple[str, ...]  # what it inserts into, alters, drops or deletes from
    reads: tuple[str, ...]
    columns: tuple[str, ...]


def _describe(tree: exp.Expression, dialect: str) -> _Described:
    from sqlglot import exp

    writers = (exp.Alter, exp.Insert, exp.Update, exp.Delete, exp.Merge)
    kind = tree.key.upper()
    target: exp.Expression | None = None
    columns: list[str] = []
    into: list[exp.Table] = []  # a materialized view's TO table, what a DROP drops
    if isinstance(tree, exp.Create):
        props = tree.args.get("properties")
        props = props.expressions if props is not None else []
        materialized = any(isinstance(p, exp.MaterializedProperty) for p in props)
        made = str(tree.args.get("kind") or "").upper()
        kind = f"CREATE {'MATERIALIZED ' if materialized else ''}{made}".strip()
        into = [
            p.this
            for p in props
            if isinstance(p, exp.ToTableProperty) and isinstance(p.this, exp.Table)
        ]
        target = tree.this
        if isinstance(target, exp.Index) and isinstance(target.args.get("table"), exp.Table):
            into = [target.args["table"]]  # an index changes the table it is built on
        if isinstance(target, exp.Schema):
            columns = [c.name for c in target.expressions if isinstance(c, exp.ColumnDef)]
            target = target.this
    elif isinstance(tree, exp.Drop):
        made = str(tree.args.get("kind") or "").upper()
        kind = f"DROP {'MATERIALIZED ' if tree.args.get('materialized') else ''}{made}".strip()
        into = [t for t in tree.args.get("tables") or [] if isinstance(t, exp.Table)]
    elif isinstance(tree, exp.TruncateTable):
        kind = "TRUNCATE"
        into = [t for t in tree.expressions if isinstance(t, exp.Table)]
    elif isinstance(tree, exp.Comment):
        kind = "COMMENT ON"  # writes the object's metadata; reads nothing
        into = [tree.this] if isinstance(tree.this, exp.Table) else []
    elif isinstance(tree, writers):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
    if isinstance(tree, exp.Alter):
        # `RENAME TO u` names what the table becomes: written, not read.
        into += [r.this for r in tree.find_all(exp.AlterRename) if isinstance(r.this, exp.Table)]
    name = ""
    if isinstance(target, exp.UserDefinedFunction) and isinstance(target.this, exp.Table):
        target = target.this  # a procedure or function: its name, not `p()`, and not a read
        name = exp.table_name(target)
    elif isinstance(target, exp.Table):
        name = exp.table_name(target)
    elif isinstance(target, exp.Expression):
        name = target.name or target.sql(dialect=dialect)
    # `USE db` names a database, not a table it reads.
    reads = [] if isinstance(tree, exp.Use) else _reads(tree, [target, *into])
    creates = isinstance(tree, exp.Create)
    # The statement's own target first, so `writes[0]` is the table it changes.
    writes = ([name] if name and not creates else []) + [exp.table_name(t) for t in into]
    return _Described(
        kind=kind,
        defines=name if creates else "",
        writes=tuple(writes),
        reads=tuple(reads),
        columns=tuple(columns),
    )


def _reads(tree: exp.Expression, skip: list[exp.Expression | None]) -> list[str]:
    """The tables `tree` reads: every table in it but those in `skip` (what it
    defines or writes) and its own CTEs, each once, in order."""
    from sqlglot import exp

    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    reads: list[str] = []
    for t in tree.find_all(exp.Table):
        table = exp.table_name(t)
        if any(t is s for s in skip) or not table or table in ctes or table in reads:
            continue
        reads.append(table)
    return reads


@functools.cache
def _parser_version() -> str:
    import sqlglot

    # Exported undeclared, which mypy --strict refuses as an attribute.
    return f"{PARSER_VERSION}+sqlglot{getattr(sqlglot, '__version__', '')}"


def _detection_prefixes(text: str) -> list[str]:
    """The windows detection reads, longest first: the whole script when it is
    within DETECT_CHARS, then prefixes ending at a `;`, up to the
    DETECT_STATEMENTS-th `;` within DETECT_CHARS, then about half as many
    statements, and so on. The `;` are found as plain characters, so a cut may
    land inside a string or a quoted body; a dialect whose tokenizer refuses one
    window is scored on the next, shorter one."""
    head = text[:DETECT_CHARS]
    ends = [i for i, ch in enumerate(head) if ch == ";"][:DETECT_STATEMENTS]
    windows = [text] if len(text) <= DETECT_CHARS else []
    n = len(ends)
    while n:
        cut = head[: ends[n - 1] + 1]
        if cut not in windows:
            windows.append(cut)
        n //= 2
    return windows or [head]


def _score(text: str, dialect: str) -> Fraction:
    """The share of the statements' text, over the dialect's first
    DETECT_STATEMENTS statements, that it turns into syntax trees. A share, not
    a count: a dialect that cannot read a quoted body splits it into many small
    statements that parse."""
    spans = [s for s in _spans(text, dialect) if s.has_tokens][:DETECT_STATEMENTS]
    total = sum(s.code_end - s.start for s in spans)
    parsed = sum(
        s.code_end - s.start
        for s in spans
        if _parse(text[s.start : s.code_end], dialect) is not None
    )
    return Fraction(parsed, total) if total else Fraction(0)


def detect_dialect(text: str) -> tuple[str, list[str]]:
    """(the chosen dialect, every candidate that tied with it). A candidate whose
    tokenizer reads a longer window ranks first, then the larger share parsed,
    so a dialect that refuses the script cannot win on a shorter cut of it.
    When none of those parses anything, the first of them is chosen with no
    candidates: it reads the script, and nothing else was detected."""
    from sqlglot.errors import TokenError

    windows = _detection_prefixes(text)
    ranks: dict[str, tuple[int, Fraction]] = {}
    for d in DETECT_DIALECTS:
        ranks[d] = (-1, Fraction(0))
        for i, window in enumerate(windows):
            try:
                ranks[d] = (len(windows) - i, _score(window, d))
            except TokenError:
                continue
            break
    best = max(ranks.values())
    tied = [d for d in DETECT_DIALECTS if ranks[d] == best]
    if best[1] <= 0:
        return tied[0], []
    return tied[0], tied


def _whole(text: str, chosen: str, lines: Lines) -> tuple[tuple[Block, ...], Coverage]:
    """A script the tokenizer cannot split: one block of its text."""
    lead = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    block = Block(
        id="b0",
        type="sql_statement",
        text=text[lead:end],
        locator=Locator(
            line_start=lines.of(lead),
            line_end=lines.of(max(lead, end - 1)),
            char_start=lead,
            char_end=end,
        ),
        attrs={"kind": "UNKNOWN"},
    )
    coverage = Coverage(
        "statements",
        1,
        0,
        failed=(Failure("b0", f"the {chosen} tokenizer cannot split it"),),
        lost=("sql_structure",),
    )
    return (block,), coverage


# One part of a qualified name: quoted (a dot inside is the name's), or bare.
_NAME_PART = re.compile(r'"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]|[^.]+')


def _last_part(name: str) -> str:
    """A name's last part without its quotes, or '' when that is the name
    itself: `analytics.events` is `events`, and `db."my.table"` and `"my.table"`
    are `my.table`, however the source quoted it (sqlglot writes `"` for every
    dialect's quoting)."""
    parts = _NAME_PART.findall(name)
    last = parts[-1] if parts else ""
    if len(last) > 1 and last[0] in '"`[':
        close = "]" if last[0] == "[" else last[0]
        last = last[1:-1].replace(close * 2, close)
    return last if last != name else ""


def _statement(text: str, n: int, s: _Span, dialect: str, lines: Lines) -> tuple[Block, bool]:
    """The block of span `s`, and whether it is a statement that failed to parse."""
    body = text[s.start : s.end]
    loc = Locator(
        line_start=lines.of(s.start),
        line_end=lines.of(s.end - 1),
        char_start=s.start,
        char_end=s.end,
    )
    attrs: dict[str, object] = {}
    if s.comments:
        attrs["comments"] = list(s.comments)
    tree = _parse(text[s.start : s.code_end], dialect) if s.has_tokens else None
    d: _Described | None = None
    if tree is not None:
        try:
            d = _describe(tree, dialect)
        except Exception:  # noqa: BLE001 — a tree sqlglot builds but cannot render is an unparsed statement, not a failed file
            d = None
    if d is None:
        attrs["kind"] = "UNKNOWN" if s.has_tokens else "COMMENT"
        keys = tuple(dict.fromkeys(s.identifiers))
    else:
        attrs["kind"] = d.kind
        if d.defines:
            attrs["defines"] = d.defines
        if d.writes:
            attrs["writes"] = list(d.writes)
        if d.reads:
            attrs["reads"] = list(d.reads)
        if d.columns:
            attrs["columns"] = list(d.columns)
        names = [d.defines, *d.writes, *d.reads]
        # A qualified name is also found by its last part, unquoted: `events` finds
        # `analytics.events`, and `my.table` finds `db.\`my.table\``.
        names += [last for name in names if (last := _last_part(name))]
        keys = tuple(dict.fromkeys(k for k in [*names, *d.columns] if k))
    block = Block(id=f"b{n}", type="sql_statement", text=body, locator=loc, keys=keys, attrs=attrs)
    return block, d is None and s.has_tokens


def extract_sql(text: str, *, dialect: str = "") -> Artifact:
    import sqlglot
    from sqlglot.errors import TokenError

    dialect = dialect.strip()  # as sqlglot reads it, so one dialect addresses one artifact
    if dialect:
        if "," in dialect:  # sqlglot's settings suffix: a dialect is declared by its name alone
            raise UnknownDialect(f"unknown SQL dialect {dialect!r}: give the dialect's name alone")
        try:
            sqlglot.Dialect.get_or_raise(dialect)
        except ValueError as e:
            raise UnknownDialect(f"unknown SQL dialect {dialect!r}") from e
        chosen, tied, source = dialect, [dialect], "declared"
    else:
        chosen, tied = detect_dialect(text)
        source = "detected" if tied else "default"
    warnings: list[str] = []
    if len(tied) > 1:
        warnings.append(f"sql_dialect_ambiguous: {', '.join(tied)}")
    attrs: dict[str, object] = {"dialect": chosen, "dialect_source": source}
    if source == "detected":
        attrs["dialect_candidates"] = tied

    blocks: tuple[Block, ...]
    lines = Lines(text)
    spans: list[_Span] | None = None
    for d in [chosen, *(t for t in tied if t != chosen)]:
        try:
            spans = _spans(text, d)
        except TokenError:
            continue
        # Past the detection windows a tied candidate's tokenizer may refuse the
        # script; the next one that splits it is used.
        chosen = d
        attrs["dialect"] = d
        break
    if spans is None:
        blocks, coverage = _whole(text, chosen, lines)
    else:
        built = [_statement(text, n, s, chosen, lines) for n, s in enumerate(spans)]
        blocks = tuple(b for b, _ in built)
        # Coverage counts statements: a span of comments alone is a block, not one.
        # A failed unit is named by its block's id, which a reader looks up.
        statements = [s.has_tokens for s in spans].count(True)
        failed = tuple(Failure(b.id, f"no {chosen} syntax tree") for b, bad in built if bad)
        coverage = Coverage(
            "statements",
            statements,
            statements - len(failed),
            failed=failed,
            lost=("sql_structure",) if failed else (),
        )
    return Artifact(
        format="sql",
        parser="loom-sql",
        parser_version=_parser_version(),
        blocks=blocks,
        coverage=coverage,
        warnings=tuple(warnings),
        options={"sql_dialect": dialect} if dialect else {},  # as `Options` names it
        attrs=attrs,
    )
