"""SQL statement facets: the functions, settings, engines, system tables, formats and
types a statement names, with the line each first appears on."""

from __future__ import annotations

import functools
import re

from server.code.domain.extract.lang_sql_model import (
    CONF_CALL_SHAPED,
    CONF_KEYWORD_BOUND,
    FACET_ENGINE,
    FACET_ERROR,
    FACET_FORMAT,
    FACET_FUNCTION,
    FACET_SETTING,
    FACET_STATEMENT,
    FACET_SYSTEM_TABLE,
    FACET_TAG,
    JINJA_PLACEHOLDER,
    Facet,
    Header,
    Statement,
)
from server.code.domain.extract.lang_sql_split import _blank_head

# Words that are followed by `(` in SQL without being a function call. In ClickHouse
# many keywords ARE functions (`if(…)`, `and(…)`, `range(…)`, `array(…)`, `tuple(…)`,
# `map(…)`, `position(…)`, `like(…)`, `any(…)`, `replace(…)`, `cast(…)`), so the list
# holds only clause and DDL words — listing `RANGE` would lose every test of `range(`
# (`tests_expecting_error` for `range` would answer `no_match`).
_SQL_KEYWORDS = frozenset(
    {
        "AS",
        "ON",
        "USING",
        "FROM",
        "WHERE",
        "SELECT",
        "INSERT",
        "INTO",
        "VALUES",
        "SETTINGS",
        "ENGINE",
        "PARTITION",
        "ORDER",
        "GROUP",
        "LIMIT",
        "BY",
        "EXISTS",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "JOIN",
        "LEFT",
        "RIGHT",
        "INNER",
        "FULL",
        "CROSS",
        "IS",
        "BETWEEN",
        "DISTINCT",
        "WITH",
        "HAVING",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        "OVER",
        "ROWS",
        "INTERVAL",
        "TABLE",
        "VIEW",
        "CREATE",
        "DROP",
        "ALTER",
        "SET",
        "FORMAT",
        "PRIMARY",
        "KEY",
        "SAMPLE",
        "TTL",
        "COMMENT",
        "DEFAULT",
        "MATERIALIZED",
        "ALIAS",
        "CODEC",
        "INDEX",
        "TYPE",
        "GRANULARITY",
        "PREWHERE",
        "FINAL",
        "TO",
        "OFFSET",
        "FILL",
        "STEP",
        "INTERPOLATE",
        "QUALIFY",
        "WINDOW",
        "DESCRIBE",
        "DESC",
        "ASC",
        "NULLS",
        "FIRST",
        "LAST",
        "COLLATE",
        "APPLY",
        "EXCLUDE",
        "COLUMNS",
        "GLOBAL",
        "PARALLEL",
        "ASOF",
        "SEMI",
        "ANTI",
        "PASTE",
        "WATCH",
        "EVENTS",
        "LIVE",
        "REFRESH",
        "EVERY",
        "AFTER",
        "RANDOMIZE",
        "FOR",
        "DEPENDS",
        "APPEND",
        "EMPTY",
        "CLUSTER",
        "SHARD",
        "REPLICA",
        "SYNC",
        "FLUSH",
        "LOGS",
        "STOP",
        "START",
        "MERGES",
        "MOVES",
        "FETCHES",
        "SENDS",
        "RELOAD",
        "DICTIONARY",
        "DICTIONARIES",
        "CONFIG",
        "DATABASE",
        "USER",
        "ROLE",
        "QUOTA",
        "POLICY",
        "PROFILE",
        "GRANT",
        "REVOKE",
        "OPTIMIZE",
        "DEDUPLICATE",
        "CHECK",
        "ATTACH",
        "DETACH",
        "RENAME",
        "EXCHANGE",
        "TRUNCATE",
        "KILL",
        "MUTATION",
        "QUERY",
        "EXPLAIN",
        "SYNTAX",
        "PLAN",
        "PIPELINE",
        "ESTIMATE",
        "AST",
        "SYSTEM",
        "SHOW",
        "PROCESSLIST",
        "TABLES",
        "DATABASES",
        "USE",
        "INSERTS",
        "FUNCTION",
        "RETURNS",
        "LAMBDA",
        "CONSTRAINT",
        "ASSUME",
        "PROJECTION",
        "MODIFY",
        "ADD",
        "CLEAR",
        "COLUMN",
        "MATERIALIZE",
        "UPDATE",
        "DELETE",
        "MOVE",
        "PART",
        "FREEZE",
        "UNFREEZE",
        "BACKUP",
        "RESTORE",
    }
)
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
# `INSERT INTO t(a, b)`, `CREATE TABLE t(...)`, `DROP TABLE IF EXISTS t(` —
# an object name followed by a column list is not a call.
_OBJECT_NAME_RE = re.compile(
    r"(?i)\b(?:INTO|TABLE|EXISTS|VIEW|DICTIONARY|DATABASE|FUNCTION|JOIN|FROM)\s+"
    r"(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)\s*\("
)
# ClickHouse data types that take arguments: `DateTime('UTC')`, `Decimal(10,
# 2)`, `Nullable(x)`. A registered vocabulary of its own (DataTypeFactory),
# so its own facet kind rather than a "function".
_TYPE_NAMES = frozenset(
    {
        "Nullable",
        "LowCardinality",
        "Array",
        "Tuple",
        "Map",
        "FixedString",
        "Decimal",
        "Decimal32",
        "Decimal64",
        "Decimal128",
        "Decimal256",
        "DateTime",
        "DateTime64",
        "Enum",
        "Enum8",
        "Enum16",
        "AggregateFunction",
        "SimpleAggregateFunction",
        "Nested",
        "Variant",
        "Dynamic",
        "JSON",
        "Object",
        "Interval",
        "IntervalSecond",
        "Time",
        "Time64",
        "QBit",
        "BFloat16",
        "Point",
        "Ring",
        "Polygon",
        "MultiPolygon",
        "LineString",
        "MultiLineString",
    }
)
FACET_TYPE = "type"
_SET_RE = re.compile(r"(?is)^\s*SET\s+(.*)$")
_SETTINGS_RE = re.compile(r"(?is)\bSETTINGS\s+(.*?)(?=\bFORMAT\b|\bINTO\s+OUTFILE\b|$)")
_ASSIGN_RE = re.compile(r"\b([A-Za-z_]\w*)\s*=")
_ENGINE_RE = re.compile(r"(?i)\bENGINE\s*=\s*([A-Za-z_]\w*)")
_SYSTEM_RE = re.compile(r"\bsystem\.([A-Za-z_]\w*)")
_FORMAT_RE = re.compile(r"(?i)\bFORMAT\s+([A-Za-z_]\w*)")
_WORD_RE = re.compile(r"\w+")


def line_of_name(stmt: Statement, name: str) -> int:
    """The file line `name` is first written on inside a statement (its leading comments
    skipped), else the statement's first line — where a reference facet or edge sits."""
    return _line_of(stmt, name)


def _line_of(stmt: Statement, name: str) -> int:
    """The file line the first mention of `name` sits on, else the
    statement's first body line."""
    head, lines = _first_name_lines(stmt.text)
    k = lines.get(name, -1)
    return stmt.start_line + k - head if k >= 0 else stmt.start_line


# A comment or a string literal, which may span lines (so a quote inside either opens
# no name; an unclosed literal runs to the end, or each of its quotes would scan the
# rest), a word or `$` name, or a quoted name's body: every name `bare_name` gives. A
# `#` comment is not known here: a quote in one can hide a quoted name's line, never a
# word's.
_NAME_TOKEN_RE = re.compile(
    r"--[^\n]*|(?s:/\*.*?(?:\*/|\Z))|'(?:[^'\\]|''|\\.)*+'?"
    r"|([\w$]+)|`([^`\n]*)`|\"([^\"\n]*)\"|\[([^\[\]\n]*)\]"
)
_BARE_TOKEN_RE = re.compile(r"[\w$]+")


# The callers read one statement's names in a row: two statements' indexes are kept.
@functools.lru_cache(maxsize=2)
def _first_name_lines(text: str) -> tuple[int, dict[str, int]]:
    """The leading comment lines and {name: the first line it is on past them}, read
    once per statement: a scan per name costs quadratic time in a statement of many
    names."""
    head = _blank_head(text)
    out: dict[str, int] = {}
    pos = sum(len(ln) + 1 for ln in text.split("\n", head)[:head])
    line = head
    for m in _NAME_TOKEN_RE.finditer(text, pos):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        if m.lastindex:
            out.setdefault(m.group(m.lastindex), line)
        # `a` in `a$b` or in `"a b"`, and the names a comment or literal spells, each on
        # its own line
        for k, part in enumerate(m.group(0).split("\n")):
            for w in [*_BARE_TOKEN_RE.findall(part), *_WORD_RE.findall(part)]:
                out.setdefault(w, line + k)
    return head, out


def _setting_names(body: str) -> list[str]:
    names: list[str] = []
    m = _SET_RE.match(body)
    if m:
        names.extend(n for n in _ASSIGN_RE.findall(m.group(1)))
    for clause in _SETTINGS_RE.findall(body):
        names.extend(n for n in _ASSIGN_RE.findall(clause))
    return names


def statement_facets(stmt: Statement) -> list[Facet]:
    """The facets of one statement, in first-seen order, deduplicated by
    (kind, name)."""
    body = stmt.body
    engines = _ENGINE_RE.findall(body)
    out: list[Facet] = [
        Facet(FACET_STATEMENT, stmt.kind, stmt.ordinal, stmt.start_line, CONF_KEYWORD_BOUND)
    ]
    seen: set[tuple[str, str]] = set()

    def add(kind: str, name: str, conf: float) -> None:
        key = (kind, name)
        if JINJA_PLACEHOLDER in name:
            return  # A template hole names nothing
        if name and key not in seen:
            seen.add(key)
            out.append(Facet(kind, name, stmt.ordinal, _line_of(stmt, name), conf))

    for name in engines:
        add(FACET_ENGINE, name, CONF_KEYWORD_BOUND)
    for name in _setting_names(body):
        add(FACET_SETTING, name, CONF_KEYWORD_BOUND)
    for name in _SYSTEM_RE.findall(body):
        add(FACET_SYSTEM_TABLE, name, CONF_KEYWORD_BOUND)
    for name in _FORMAT_RE.findall(body):
        add(FACET_FORMAT, name, CONF_KEYWORD_BOUND)
    for name in stmt.hint.errors:
        add(FACET_ERROR, name, CONF_KEYWORD_BOUND)
    skip = set(engines) | set(_OBJECT_NAME_RE.findall(body))
    for name in _CALL_RE.findall(body):
        if name.upper() in _SQL_KEYWORDS or name in skip or len(name) < 2:
            continue
        if name in _TYPE_NAMES:
            add(FACET_TYPE, name, CONF_KEYWORD_BOUND)
        else:
            add(FACET_FUNCTION, name, CONF_CALL_SHAPED)
    return out


def file_facets(header: Header, statements: list[Statement]) -> list[Facet]:
    out = [Facet(FACET_TAG, t, 0, 1, CONF_KEYWORD_BOUND) for t in header.tags]
    for st in statements:
        out.extend(statement_facets(st))
    return out
