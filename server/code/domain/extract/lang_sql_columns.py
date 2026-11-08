"""Column-level lineage for SQL.

The SQL-object edges give the plane the table a statement reads or writes; this module
gives it the COLUMN, where the text makes the binding unambiguous:

  * **Definitions** — `CREATE TABLE t (a UInt64, b String, …)` and
    `ALTER TABLE t ADD COLUMN c …` define columns; `ALTER TABLE t DROP
    COLUMN c`, `RENAME COLUMN a TO b`, `MODIFY / ALTER COLUMN c …` write
    them. A column is a `column` symbol named `<table>.<column>` under the
    file's module scope (`mod::<schema>.<table>.<column>`), one per defined
    column, so `code.symbol("users.email")` finds the definition and
    `code.callers("users.email")` the statements that touch it.
  * **References** — a qualified identifier `x.col` whose qualifier is a
    table or an alias of the statement's FROM / JOIN / UPDATE / INSERT
    target binds to that table; an unqualified identifier binds only when
    the statement touches exactly ONE table and the column is a known
    column of it (`known`: the file's own definitions plus what the caller
    learned from the namespace). `INSERT INTO t (a, b)` writes `a`, `b`;
    `UPDATE t SET a = …` / `ALTER TABLE t UPDATE a = …` writes `a`; every
    other reference is a read. `SELECT *` over one table reads its known columns
    (`CONF_STAR`), for a table of at most `STAR_COLUMNS_MAX` of them and up to
    `STAR_READS_MAX` columns a file.
  * **Facets** — `column_read` / `column_write` rows named
    `<table>.<column>` on the statement (the facet table), so
    `code.tests_for({facets:[{kind:"column_write",name:"users.email"}]})`
    answers; **edges** — statement → column symbol `reads` / `writes`
    (the read/write edge kinds) resolved own-file first, then repository-
    unique by the `<table>.<column>` name.

What is deliberately NOT bound (schema-less column lineage cannot settle these; lineage
conventions mark such edges as indirect): an unqualified column in a multi-table
statement, a column through a subquery or CTE alias, a `*` over several tables, a column
of a table nobody in the tree defines. Precision over coverage — a wrong column edge
misroutes an impact question, a missing one only widens it to the table edge already
drawn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_sql import statement_symbol
from server.code.domain.extract.lang_sql_facets import line_of_name
from server.code.domain.extract.lang_sql_model import Facet, Statement
from server.code.domain.extract.lang_sql_objects import (
    _BUILTIN_FUNCTIONS,
    EDGE_READS,
    EDGE_WRITES,
    CreatedObject,
    References,
    _plain,
    bare_name,
    created_object,
    references,
    split_schema,
)
from server.code.domain.extract.lang_sql_split import comment_spans, without_spans
from server.code.domain.extract.treesitter import Symbol

KIND_COLUMN = "column"
FACET_COLUMN_READ = "column_read"
FACET_COLUMN_WRITE = "column_write"
CONF_QUALIFIED = 0.95  # `alias.col` → the alias's table
CONF_SINGLE_TABLE = 0.9  # unqualified `col` in a one-table statement, column known
CONF_EXPLICIT = 1.0  # `INSERT INTO t (a, b)`, `UPDATE t SET a`

# A bare name starts a word and a bracketed one holds no `[`: either is then searched
# once, not again from every later character or `[`.
_IDENT = r"(?:`[^`]+`|\"[^\"]+\"|\[[^\[\]]+\]|(?<![\w$])[A-Za-z_][\w$]*)"
# a column definition item's head: the column name then its type word
_COL_DEF_RE = re.compile(r"^\s*(" + _IDENT + r")\s+(?:(" + _IDENT + r")|\()")
# items of a CREATE TABLE body that are not columns
_NOT_A_COLUMN = frozenset(
    {
        "PRIMARY",
        "FOREIGN",
        "UNIQUE",
        "KEY",
        "INDEX",
        "CONSTRAINT",
        "CHECK",
        "PROJECTION",
        "EXCLUDE",
        "LIKE",
        "PERIOD",
        "TTL",
        "ORDER",
        "PARTITION",
        "FULLTEXT",
        "SPATIAL",
        "CLUSTERED",
        "NONCLUSTERED",
        "WITH",
        "SYSTEM",
        "SETTINGS",
        "ENGINE",
        "COMMENT",
        "AS",
    }
)
_CREATE_TABLE_RE = re.compile(
    r"(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:TEMP|TEMPORARY|UNLOGGED|GLOBAL|LOCAL|EXTERNAL"
    r"|FOREIGN|REPLICATED)\s+)*TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    + _IDENT
    + r"(?:\."
    + _IDENT
    + r"){0,2}\s*(?:ON\s+CLUSTER\s+\S+\s*)?(?:UUID\s+'[^']*'\s*)?\("
)
_ALTER_COLUMN_RE = re.compile(
    r"(?i)\b(ADD|DROP|MODIFY|ALTER|RENAME|COMMENT|CLEAR|MATERIALIZE)\s+COLUMN\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(" + _IDENT + r")(?:\s+TO\s+(" + _IDENT + r"))?"
)
_ALTER_TABLE_RE = re.compile(
    r"(?is)^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?("
    + _IDENT
    + r"(?:\."
    + _IDENT
    + r"){0,2})"
)
_ALTER_UPDATE_RE = re.compile(
    r"(?is)^\s*+ALTER\s++TABLE\s++(?:(?!\bUPDATE\s).)*?\bUPDATE\s++(.*?)(?<=\S)\s+WHERE\b"
)
# unanchored: a `CREATE RULE … DO INSTEAD INSERT INTO t (…)` or a function body writes
# its columns as explicitly as a top-level statement
_UPDATE_SET_RE = re.compile(
    r"(?is)\bUPDATE\s+(?:ONLY\s+)?(" + _IDENT + r"(?:\." + _IDENT + r"){0,2})"
    r"(?:\s+(?:AS\s+)?(" + _IDENT + r"))?\s+SET\s++(.*?)(?:(?<=\S)\s+WHERE\b|(?<=\S)\s+FROM\b|;|$)"
)
_INSERT_COLS_RE = re.compile(
    r"(?is)\b(?:INSERT|REPLACE)\s+INTO\s+(?:TABLE\s+)?("
    + _IDENT
    + r"(?:\."
    + _IDENT
    + r"){0,2})\s*\(([^()]*)\)"
)
CONF_STAR = 0.85  # `SELECT *` over a one-table statement whose columns are known
# A `SELECT *` over a wider table reads nothing, and the stars of one file read at most
# STAR_READS_MAX columns together, or a file of one wide table and many `SELECT *` would
# read columns times statements.
STAR_COLUMNS_MAX = 1000  # not a setting: bounds what one star reads
STAR_READS_MAX = 100_000  # not a setting: bounds what one file's stars read


@dataclass
class StarBudget:
    """The columns the `SELECT *` of one file may still read."""

    left: int = STAR_READS_MAX


_STAR_RE = re.compile(
    r"(?is)\bSELECT\s+(?:DISTINCT\s+)?(?:(" + _IDENT + r")\.)?\*(?=\s*(?:,|FROM\b))"
)
_WITH_RE = re.compile(
    r"(?is)(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)(" + _IDENT + r")(?:\s*\([^()]*\))?\s+AS\s*\("
)
_DERIVED_RE = re.compile(r"(?is)\)\s*(?:AS\s+)?(" + _IDENT + r")\b")
_SELECT_LIST_RE = re.compile(r"(?is)^\s*+SELECT\s++(?:DISTINCT\s++)?(.*?)(?<=\S)\s+FROM\b")
_ASSIGN_LHS_RE = re.compile(r"(" + _IDENT + r"(?:\." + _IDENT + r")?)\s*=")
# `table [AS] alias` after FROM / JOIN / UPDATE / INTO
_SOURCE_RE = re.compile(
    r"(?i)\b(?:FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\s+("
    + _IDENT
    + r"(?:\."
    + _IDENT
    + r"){0,2})(?!\s*\()(?:\s+(?:AS\s+)?(?!(?:WHERE|SET|ON|USING|JOIN|LEFT|RIGHT|INNER|FULL|CROSS"
    r"|GROUP|ORDER|LIMIT|HAVING|UNION|FINAL|SAMPLE|PREWHERE|SETTINGS|FORMAT|VALUES|SELECT|WITH"
    r"|ASOF|SEMI|ANTI|ANY|ALL|GLOBAL|ARRAY|PARTITION|RETURNING|ONLY|INTO|WINDOW|QUALIFY)\b)("
    + _IDENT
    + r"))?"
)
# `, table [AS] alias` continuations of a comma-joined FROM list
_LIST_ALIAS_RE = re.compile(
    r"(?i),\s*(" + _IDENT + r"(?:\." + _IDENT + r"){0,2})(?!\s*\()"
    r"(?:\s+(?:AS\s+)?(?!(?:WHERE|SET|ON"
    r"|USING|JOIN|LEFT|RIGHT|INNER|FULL|CROSS|GROUP|ORDER|LIMIT|HAVING|UNION|FINAL|SAMPLE|PREWHERE"
    r"|SETTINGS|FORMAT|VALUES|SELECT|WITH|WINDOW|QUALIFY)\b)(" + _IDENT + r"))?"
)
_ALTER_WHERE_RE = re.compile(
    r"(?is)^\s*+ALTER\s++TABLE\s++(?:(?!\b(?:UPDATE|DELETE)\b).)*?\b(?:UPDATE|DELETE)\b.*?\bWHERE\b(.*)$"
)
_QUALIFIED_RE = re.compile(r"(?<![\w$.\"`\]])(" + _IDENT + r")\.(" + _IDENT + r")(?![\w$.(])")
_BARE_RE = re.compile(r"(?<![\w$.\"`\]@:{])(" + _IDENT + r")(?![\w$.(`\"])")
_STRING_RE = re.compile(r"'(?:[^']|'')*'|\$\$.*?\$\$", re.S)
_KEYWORDS = frozenset(
    w.upper()
    for w in """
select from where and or not in is null as on join left right inner full cross outer using
group by order having limit offset union all distinct case when then else end between like
ilike exists insert into values update set delete truncate create table view if drop alter add
column columns modify rename to with recursive final sample prewhere settings format asc desc
nulls first last over partition rows range preceding following current row unbounded window
qualify returning default primary key index engine ttl comment codec materialized alias ephemeral
true false interval cast array tuple map any global asof semi anti paste array_join arrayjoin
top percent fetch next only lateral natural except intersect minus escape collate
attach detach optimize system show describe explain grant revoke replace merge match
count sum min max avg
""".split()
)
# type words seen after a column in DDL — never a column reference
_TYPE_WORDS = frozenset(
    w.upper()
    for w in """
int integer bigint smallint tinyint uint8 uint16 uint32 uint64 int8 int16 int32 int64 float
float32 float64 double decimal numeric string text varchar char nchar nvarchar date datetime
datetime64 timestamp timestamptz time bool boolean uuid json jsonb bytea blob serial bigserial
array nullable lowcardinality tuple map nested enum enum8 enum16 fixedstring ipv4 ipv6 real
money bit binary varbinary clob nclob number varchar2 date32 object dynamic variant
""".split()
)


@dataclass(frozen=True)
class ColumnDef:
    table: str  # bare table name
    schema: str  # the table's schema as written or from context, '' if none
    column: str
    type: str = ""

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.key}" if self.schema else self.key


@dataclass
class ColumnRefs:
    reads: list[tuple[str, str, float]] = field(default_factory=list)  # (table, column, conf)
    writes: list[tuple[str, str, float]] = field(default_factory=list)


def column_lineage_on() -> bool:
    """The column-lineage gate, read where the SQL lanes assemble their rows."""
    from server.settings import settings

    return bool(settings.code_sql_column_lineage)


def _split_top_level(body: str) -> list[str]:
    """Split a parenthesised body on commas outside nested parens / quotes."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    quote = ""
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"`":
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _paren_closes(text: str) -> dict[int, int]:
    """{index of a `(`: index of its `)`} for the balanced parens outside quotes, in one
    pass."""
    out: dict[int, int] = {}
    opens: list[int] = []
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            opens.append(i)
        elif ch == ")" and opens:
            out[opens.pop()] = i
    return out


def table_columns(stmt: Statement, obj: CreatedObject | None = None) -> list[ColumnDef]:
    """The columns a CREATE TABLE statement defines, in order; [] for a
    CREATE TABLE … AS SELECT without a column list, a CREATE TABLE … AS t,
    an ENGINE-only body, or any other object kind."""
    obj = obj if obj is not None else created_object(stmt)
    if obj is None or obj.kind != "table":
        return []
    m = _CREATE_TABLE_RE.match(stmt.body)
    if not m:
        return []
    close = _paren_closes(stmt.body[m.end() - 1 :]).get(0)  # quotes read from the `(`
    if close is None:
        return []
    inner = stmt.body[m.end() : m.end() - 1 + close]
    out: list[ColumnDef] = []
    seen: set[str] = set()
    for item in _split_top_level(inner):
        cm = _COL_DEF_RE.match(item)
        if not cm:
            continue
        name = _plain(cm.group(1))
        if name.upper() in _NOT_A_COLUMN or name.upper() in _KEYWORDS - {"KEY", "COMMENT"}:
            continue
        type_word = _plain(cm.group(2) or "")
        if type_word.upper() in ("REFERENCES", "AS", "ON", "TO") and name.upper() in _NOT_A_COLUMN:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(ColumnDef(obj.bare, obj.schema, name, type_word))
    return out


def column_changes(
    stmt: Statement, context_schema: str = ""
) -> tuple[list[ColumnDef], list[ColumnDef]]:
    """(added, written) columns of an ALTER TABLE: `ADD COLUMN c T` defines
    `c`; DROP / MODIFY / ALTER / RENAME / COMMENT / CLEAR / MATERIALIZE
    COLUMN write the named column (RENAME also defines the new name)."""
    m = _ALTER_TABLE_RE.match(stmt.body)
    if not m:
        return [], []
    schema, table = split_schema(m.group(1))
    schema = schema or context_schema
    added: list[ColumnDef] = []
    written: list[ColumnDef] = []
    for op, col, to in _ALTER_COLUMN_RE.findall(stmt.body):
        c = ColumnDef(table, schema, _plain(col))
        if op.upper() == "ADD":
            added.append(c)
        else:
            written.append(c)
            if op.upper() == "RENAME" and to:
                added.append(ColumnDef(table, schema, _plain(to)))
    return added, written


def file_columns(
    statements: list[Statement], contexts: dict[int, str] | None = None
) -> dict[str, set[str]]:
    """{bare table: {column}} the file itself defines (CREATE TABLE bodies
    and ADD / RENAME COLUMN), the oracle for unqualified references."""
    known: dict[str, set[str]] = {}
    for st in statements:
        ctx = (contexts or {}).get(st.ordinal, "")
        for c in table_columns(st, created_object(st, ctx)):
            known.setdefault(c.table, set()).add(c.column)
        added, _w = column_changes(st, ctx)
        for c in added:
            known.setdefault(c.table, set()).add(c.column)
    return known


def _strip(body: str) -> str:
    """The statement body without string literals and comments (their text
    is not identifiers), same length not required."""
    return _STRING_RE.sub("''", without_spans(body, comment_spans(body, True), " "))


def _alias_map(body: str, refs: References) -> dict[str, str]:
    """{alias or written name (lower): bare table} for the statement's
    sources, CTE names excluded through `refs` (a CTE is not a table)."""
    tables = {bare_name(n).lower(): bare_name(n) for n in [*refs.reads, *refs.writes]}
    out: dict[str, str] = dict(tables)
    list_end = 0
    for m in _SOURCE_RE.finditer(body):
        written, alias = m.group(1), m.group(2) or ""
        pairs: list[tuple[str, str]] = [(written, alias)]
        # the comma-joined list that may follow: `FROM a x, b AS y, c`; a source inside
        # a list already read adds only itself, or a long list is read once per item
        pos = m.end()
        while m.start() >= list_end:
            lm = _LIST_ALIAS_RE.match(body, pos)
            if lm is None:
                list_end = pos
                break
            pairs.append((lm.group(1), lm.group(2) or ""))
            pos = lm.end()
        for written, alias in pairs:
            bare = bare_name(written)
            if bare.lower() not in tables:
                continue
            out[_plain(written).lower()] = bare
            if alias and _plain(alias).upper() not in _KEYWORDS:
                out[_plain(alias).lower()] = bare
    return out


def _known_has(known: dict[str, set[str]], table: str, col: str) -> bool:
    cols = known.get(table)
    return cols is not None and col in cols


class _Collector:
    """De-duplicating sink for (table, column, confidence) bindings."""

    def __init__(self) -> None:
        self.out = ColumnRefs()
        self._seen: set[tuple[str, str, str]] = set()

    def add(self, tag: str, table: str, col: str, conf: float) -> None:
        col = _plain(col)
        if not col or col.upper() in _KEYWORDS or col == "*" or (tag, table, col) in self._seen:
            return
        self._seen.add((tag, table, col))
        (self.out.writes if tag == "w" else self.out.reads).append((table, col, conf))


def _explicit_writes(body: str, stmt: Statement, acc: _Collector) -> str:
    """`INSERT INTO t (a, b)`, `UPDATE t SET a = …`, `ALTER TABLE t UPDATE a
    = …` and the ALTER COLUMN forms write their columns at 1.0; returns the
    UPDATE's SET clause (its left-hand sides are not reads)."""
    for im in _INSERT_COLS_RE.finditer(body):
        table = bare_name(im.group(1))
        for col in im.group(2).split(","):
            acc.add("w", table, col.strip(), CONF_EXPLICIT)
    set_clause = ""
    for um in _UPDATE_SET_RE.finditer(body):
        table = bare_name(um.group(1))
        set_clause = set_clause or um.group(3)
        for lhs in _ASSIGN_LHS_RE.findall(um.group(3)):
            acc.add("w", table, lhs.split(".")[-1], CONF_EXPLICIT)
    am = _ALTER_UPDATE_RE.match(body)
    tm = _ALTER_TABLE_RE.match(body) if am else None
    if am and tm:
        table = bare_name(tm.group(1))
        for lhs in _ASSIGN_LHS_RE.findall(am.group(1)):
            acc.add("w", table, lhs.split(".")[-1], CONF_EXPLICIT)
    _added, altered = column_changes(stmt)
    for c in altered:
        acc.add("w", c.table, c.column, CONF_EXPLICIT)
    return set_clause


def _bare_reads(
    text: str, table: str, known: dict[str, set[str]], not_columns: set[str], acc: _Collector
) -> None:
    """Unqualified identifiers of a one-table statement that are known
    columns of that table — never a keyword, type word, function, the
    table's own name or an alias."""
    if not known.get(table):
        return
    for col in _BARE_RE.findall(text):
        plain = _plain(col)
        if (
            plain.upper() in _KEYWORDS
            or plain.upper() in _TYPE_WORDS
            or plain in _BUILTIN_FUNCTIONS
            or plain.lower() in not_columns
        ):
            continue
        if _known_has(known, table, plain):
            acc.add("r", table, plain, CONF_SINGLE_TABLE)


def _select_items(select_body: str) -> list[tuple[str, str, str]]:
    """(output name, qualifier, source column) per select-list item of a
    SELECT body: `a` → (a, '', a); `t.a` → (a, t, a); `t.a AS x` / `a x` →
    (x, t|'', a); an expression → ('', '', '') (nothing to carry)."""
    m = _SELECT_LIST_RE.match(select_body)
    if not m:
        return []
    out: list[tuple[str, str, str]] = []
    for item in _split_top_level(m.group(1)):
        item = item.strip()
        if not item or item == "*" or "(" in item or item.endswith("*"):
            out.append(("", "", ""))
            continue
        alias = ""
        am = re.match(r"(?is)^(.*?)(?<=\S)\s+(?:AS\s+)?(" + _IDENT + r")$", item)
        expr = item
        if am and am.group(2).upper() not in _KEYWORDS:
            expr, alias = am.group(1).strip(), _plain(am.group(2))
        qm = re.fullmatch(r"(?:(" + _IDENT + r")\.)?(" + _IDENT + r")", expr)
        if not qm or _plain(qm.group(2)).upper() in _KEYWORDS:
            out.append(("", "", ""))
            continue
        col = _plain(qm.group(2))
        out.append((alias or col, _plain(qm.group(1) or "").lower(), col))
    return out


def _body_columns(
    select_body: str,
    known: dict[str, set[str]],
    scoped: dict[str, dict[str, tuple[str, str]]] | None = None,
) -> dict[str, tuple[str, str]]:
    """{output column: (table, source column)} a CTE or derived-table body exposes,
    where the source binds through the body's own aliases, its one known table, or an
    earlier CTE it selects from (the columns then carry that CTE's sources); everything
    else is unmapped."""
    from server.code.domain.extract.lang_sql_split import split_statements

    sts = split_statements(select_body)
    if not sts:
        return {}
    st = sts[0]
    refs = references(st)
    body = _strip(st.body)
    scoped = scoped or {}
    # an earlier CTE the body reads is a scope, not a table (the body alone
    # has no WITH, so `references` took its name for a table)
    aliases = {a: t for a, t in _alias_map(body, refs).items() if t.lower() not in scoped}
    tables = {bare_name(n) for n in refs.reads if bare_name(n).lower() not in scoped}
    sources = _source_names(body)
    cte_sources = [s for s in sources if s in scoped]
    out: dict[str, tuple[str, str]] = {}
    for name, qual, col in _select_items(body):
        if not name:
            continue
        if qual and qual in scoped:
            hit = scoped[qual].get(col.lower())
            if hit:
                out[name.lower()] = hit
            continue
        table = aliases.get(qual) if qual else (next(iter(tables)) if len(tables) == 1 else None)
        if table is None:
            if not qual and not tables and len(cte_sources) == 1:
                hit = scoped[cte_sources[0]].get(col.lower())
                if hit:
                    out[name.lower()] = hit
            continue
        if qual or _known_has(known, table, col):
            out[name.lower()] = (table, col)
    return out


def _source_names(body: str) -> list[str]:
    """The written names after FROM / JOIN (lower), tables and CTEs alike."""
    return [_plain(m.group(1)).lower() for m in _SOURCE_RE.finditer(body)]


def _scope_aliases(body: str, scoped: dict[str, dict[str, tuple[str, str]]]) -> dict[str, str]:
    """{alias: scope name} for `FROM recent AS r2` where `recent` is a CTE or derived
    table (a re-aliased CTE keeps its columns)."""
    out: dict[str, str] = {}
    for m in _SOURCE_RE.finditer(body):
        written, alias = _plain(m.group(1)).lower(), _plain(m.group(2) or "").lower()
        if written in scoped and alias and alias.upper() not in _KEYWORDS:
            out[alias] = written
    return out


def _scoped_columns(body: str, known: dict[str, set[str]]) -> dict[str, dict[str, tuple[str, str]]]:
    """{cte or derived-table alias (lower): its exposed columns} for the
    `WITH name AS (…)` and `(SELECT …) alias` scopes of a statement."""
    out: dict[str, dict[str, tuple[str, str]]] = {}
    closes = _paren_closes(body)
    # Each scope around a nested one reads its body again: the bodies read are capped at
    # 4 times the statement, so a deep nest costs linear time.
    budget = 4 * len(body)
    for m in _WITH_RE.finditer(body):
        close = closes.get(m.end() - 1)
        if close is not None and close - m.end() <= budget:
            budget -= close - m.end()
            inner = body[m.end() : close]
            out[_plain(m.group(1)).lower()] = _body_columns(inner, known, out)  # in order
    depth = 0
    for i, ch in enumerate(body):
        if ch == "(":
            if depth == 0 and re.match(r"(?is)\(\s*SELECT\b", body[i : i + 12]):
                close = closes.get(i)
                if close is not None:
                    dm = _DERIVED_RE.match(body, close)
                    if dm and _plain(dm.group(1)).upper() not in _KEYWORDS:
                        derived = body[i + 1 : close]
                        out[_plain(dm.group(1)).lower()] = _body_columns(derived, known, out)
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
    return out


def _star_reads(
    body: str,
    tables: set[str],
    aliases: dict[str, str],
    known: dict[str, set[str]],
    acc: _Collector,
    budget: StarBudget,
) -> None:
    """`SELECT *` / `SELECT t.*` over a table whose columns are known, at most
    `STAR_COLUMNS_MAX` of them, reads every known column at CONF_STAR, while the
    file's budget lasts."""
    for sm in _STAR_RE.finditer(body):
        qual = _plain(sm.group(1) or "").lower()
        table = aliases.get(qual) if qual else (next(iter(tables)) if len(tables) == 1 else None)
        if table is None:
            continue
        cols = known.get(table) or ()
        if len(cols) > min(STAR_COLUMNS_MAX, budget.left):
            continue
        budget.left -= len(cols)
        for col in sorted(cols):
            acc.add("r", table, col, CONF_STAR)


def column_refs(
    stmt: Statement,
    known: dict[str, set[str]],
    refs: References | None = None,
    star: StarBudget | None = None,
) -> ColumnRefs:
    """The (table, column, confidence) a statement reads and writes, bound
    by the rules in the module docstring. `known` is {bare table: columns}."""
    refs = refs if refs is not None else references(stmt)
    acc = _Collector()
    body = _strip(stmt.body)
    if refs.creates is not None and refs.creates.kind == "table" and not refs.reads:
        return acc.out  # a definition names its own columns, not references
    set_clause = _explicit_writes(body, stmt, acc)
    aliases = _alias_map(body, refs)
    tables = {bare_name(n) for n in [*refs.reads, *refs.writes]}
    not_columns = set(aliases) | {t.lower() for t in tables}
    if stmt.kind.upper().startswith("ALTER"):
        # an ALTER's columns are explicit (handled above) except the WHERE of
        # a mutation (`ALTER TABLE t UPDATE a = 1 WHERE b > 0`): scan that
        wm = _ALTER_WHERE_RE.match(body)
        if wm and len(tables) == 1:
            _bare_reads(wm.group(1), next(iter(tables)), known, not_columns, acc)
        return _drop_written_reads(acc.out)
    if stmt.kind.upper().startswith(("INSERT", "REPLACE")):
        # INSERT … SELECT: the unqualified columns are the SELECT's, from
        # its source tables — the target's columns are the explicit list;
        # a SELECT over no table (`generate_series`, `numbers`) names none
        tables = {bare_name(n) for n in refs.reads}
    # the SET clause's left-hand sides are writes, not reads
    read_body = body
    if set_clause:
        read_body = body.replace(set_clause, _ASSIGN_LHS_RE.sub("=", set_clause), 1)
    scoped = _scoped_columns(body, known)  # CTEs and derived tables
    for alias, scope in _scope_aliases(body, scoped).items():  # `recent AS r2`
        scoped.setdefault(alias, scoped[scope])
    for qual, col in _QUALIFIED_RE.findall(read_body):
        q = _plain(qual).lower()
        c = _plain(col)
        if c.upper() in _KEYWORDS:
            continue
        table = aliases.get(q)
        if table is not None:
            acc.add("r", table, c, CONF_QUALIFIED)
        elif q in scoped and c.lower() in scoped[q]:
            t, src = scoped[q][c.lower()]
            acc.add("r", t, src, CONF_QUALIFIED)
    if len(tables) == 1:
        _bare_reads(read_body, next(iter(tables)), known, not_columns, acc)
    elif not tables and len(scoped) == 1:
        # `WITH c AS (SELECT a, b FROM t) SELECT a FROM c`: the CTE's columns
        ((_alias, cols),) = scoped.items()
        for col in _BARE_RE.findall(read_body):
            plain = _plain(col)
            if plain.upper() not in _KEYWORDS and plain.lower() in cols:
                t, src = cols[plain.lower()]
                acc.add("r", t, src, CONF_SINGLE_TABLE)
    _star_reads(read_body, tables, aliases, known, acc, star or StarBudget())
    return _drop_written_reads(acc.out)


def _drop_written_reads(out: ColumnRefs) -> ColumnRefs:
    """A column written is not also "read" through its own SET / list mention."""
    written = {(t, c) for t, c, _ in out.writes}
    out.reads = [(t, c, k) for t, c, k in out.reads if (t, c) not in written]
    return out


def column_symbol(path: str, mod: str, col: ColumnDef, line: int) -> Symbol:
    qn = f"{mod}::{col.qualified}"
    sig = f"column {col.qualified}" + (f" {col.type}" if col.type else "")
    return Symbol(
        qualified_name=qn,
        name=col.key,
        kind=KIND_COLUMN,
        signature=sig,
        path=path,
        start_line=line,
        end_line=line,
    )


def column_symbols(
    path: str, mod: str, statements: list[Statement], contexts: dict[int, str] | None = None
) -> list[Symbol]:
    """One `column` symbol per column the file defines (CREATE TABLE bodies,
    ADD / RENAME COLUMN), on the line the column is written."""
    out: dict[str, Symbol] = {}
    for st in statements:
        ctx = (contexts or {}).get(st.ordinal, "")
        defs = table_columns(st, created_object(st, ctx))
        added, _w = column_changes(st, ctx)
        for c in [*defs, *added]:
            if c.qualified in out:
                continue
            out[c.qualified] = column_symbol(path, mod, c, line_of_name(st, c.column))
    return list(out.values())


def column_facets(statements: list[Statement], known: dict[str, set[str]]) -> list[Facet]:
    """`column_read` / `column_write` facets named `<table>.<column>` on the
    statement that references the column."""
    out: list[Facet] = []
    star = StarBudget()  # one per file
    for st in statements:
        cr = column_refs(st, known, star=star)
        for table, col, conf in cr.reads:
            line = line_of_name(st, col)
            out.append(Facet(FACET_COLUMN_READ, f"{table}.{col}", st.ordinal, line, conf))
        for table, col, conf in cr.writes:
            line = line_of_name(st, col)
            out.append(Facet(FACET_COLUMN_WRITE, f"{table}.{col}", st.ordinal, line, conf))
    return out


def column_edges(
    path: str,
    mod: str,
    statements: list[Statement],
    known: dict[str, set[str]],
    own: list[Symbol],
    name_table: dict[str, list[tuple[str, str, str]]],
    *,
    rw_kinds: bool = False,
) -> list[Edge]:
    """Statement → `column` symbol edges: the file's own column (1.0 × the
    binding's confidence) first, else the one column symbol in the
    repository named `<table>.<column>` (0.9 ×)."""
    own_by_key: dict[str, Symbol] = {s.name: s for s in own if s.kind == KIND_COLUMN}
    edges: list[Edge] = []
    star = StarBudget()  # one per file
    for st in statements:
        cr = column_refs(st, known, star=star)
        if not cr.reads and not cr.writes:
            continue
        src = statement_symbol(path, mod, st)
        for bucket, kind in ((cr.writes, EDGE_WRITES), (cr.reads, EDGE_READS)):
            for table, col, conf in bucket:
                key = f"{table}.{col}"
                hit = own_by_key.get(key)
                to_id, to_qn, scale = (hit.id, hit.qualified_name, 1.0) if hit else ("", "", 0.9)
                if not hit:
                    cands = [c for c in name_table.get(key, []) if c[2] == KIND_COLUMN]
                    if len({c[0] for c in cands}) != 1:
                        continue
                    to_id, to_qn = cands[0][0], cands[0][1]
                edges.append(
                    Edge(
                        edge_kind=kind if rw_kinds else "calls",
                        from_symbol_id=src.id,
                        to_symbol_id=to_id,
                        from_qualified_name=src.qualified_name,
                        to_qualified_name=to_qn,
                        path=path,
                        line=line_of_name(st, col),
                        confidence=round(conf * scale, 3),
                    )
                )
    return edges


def column_hypotheses(statements: list[Statement], known: dict[str, set[str]]) -> set[str]:
    """`<table>.<identifier>` names a single-table statement MIGHT be
    referencing for a table the file does not define itself: what the
    ingest asks the name table for, so a `column` symbol another file
    defines (the migration that created the table) lets the unqualified
    reference bind. Capped per statement; keywords and functions excluded."""
    out: set[str] = set()
    for st in statements:
        refs = references(st)
        if refs.creates is not None and refs.creates.kind == "table" and not refs.reads:
            continue
        tables = {bare_name(n) for n in [*refs.reads, *refs.writes]}
        if stmt_is_insert(st):
            tables = {bare_name(n) for n in refs.reads}
        if len(tables) != 1:
            continue
        (table,) = tables
        if known.get(table):
            continue  # the file defines it: `file_columns` already knows
        body = _strip(st.body)
        idents = {
            _plain(c)
            for c in _BARE_RE.findall(body)
            if _plain(c).upper() not in _KEYWORDS
            and _plain(c).upper() not in _TYPE_WORDS
            and _plain(c) not in _BUILTIN_FUNCTIONS
            and _plain(c).lower() != table.lower()
        }
        out.update(f"{table}.{c}" for c in sorted(idents)[:40])
    return out


def stmt_is_insert(st: Statement) -> bool:
    return st.kind.upper().startswith(("INSERT", "REPLACE"))


def known_from_name_table(
    name_table: dict[str, list[tuple[str, str, str]]], hypotheses: set[str]
) -> dict[str, set[str]]:
    """{table: columns} the namespace confirms among `hypotheses`: a name
    with exactly one `column` symbol candidate is a known column."""
    out: dict[str, set[str]] = {}
    for key in hypotheses:
        cands = {c[0] for c in name_table.get(key, []) if c[2] == KIND_COLUMN}
        if len(cands) == 1:
            table, col = key.rsplit(".", 1)
            out.setdefault(table, set()).add(col)
    return out


def merge_known(*parts: dict[str, set[str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for part in parts:
        for t, cols in part.items():
            out.setdefault(t, set()).update(cols)
    return out


def column_names(statements: list[Statement], known: dict[str, set[str]]) -> set[str]:
    """The `<table>.<column>` names the file references — what the ingest
    asks the name table for before `column_edges`."""
    out: set[str] = set()
    star = StarBudget()  # one per file
    for st in statements:
        cr = column_refs(st, known, star=star)
        out.update(f"{t}.{c}" for t, c, _ in [*cr.reads, *cr.writes])
    return out


__all__ = [
    "CONF_EXPLICIT",
    "CONF_QUALIFIED",
    "CONF_SINGLE_TABLE",
    "CONF_STAR",
    "FACET_COLUMN_READ",
    "FACET_COLUMN_WRITE",
    "KIND_COLUMN",
    "ColumnDef",
    "ColumnRefs",
    "column_changes",
    "column_edges",
    "column_facets",
    "column_hypotheses",
    "column_names",
    "column_refs",
    "column_lineage_on",
    "column_symbols",
    "file_columns",
    "known_from_name_table",
    "merge_known",
    "table_columns",
]
