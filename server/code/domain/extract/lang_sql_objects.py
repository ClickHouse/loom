"""The objects a SQL file creates, and the tables its statements read and write.

`CREATE TABLE users (…)` is a definition the way `def users()` is: a name a reader asks
for by `code.symbol`, jumps to by `code.outline`, and wants the users of by
`code.callers` ("who reads table X, who writes it"). ctags' SQL parser lists these
kinds (table, view, index, function, procedure, trigger, type, schema, database,
sequence, …); without them a SQL file's symbols would be its statement records alone,
and no edge would leave a SQL file except a psql include.

This module reads, per statement record:

  * the object a `CREATE …` statement defines → one symbol of that kind
    (`<module>::<name>`, the statement's span, its leading comment as doc);
  * the tables a statement reads (`FROM t`, `JOIN t`, `SHOW CREATE TABLE
    t`, `DESCRIBE t`, `COPY t TO`) and writes (`INSERT INTO t`, `UPDATE t`,
    `DELETE FROM t`, `ALTER TABLE t`, `DROP TABLE t`, `TRUNCATE t`,
    `OPTIMIZE TABLE t`, `CREATE INDEX … ON t`, `CREATE MATERIALIZED VIEW
    … TO t`, `COPY t FROM`, …) → `table_read` / `table_write` facets, and
    a `creates` facet for the defined object;
  * the edges: from the statement record to the object it references,
    resolved to the file's own definitions first, then to a repository-
    unique one through the name table (a ClickHouse test's `t` never
    binds to another test's `t`). A table reference is a `reads` or
    `writes` edge under `rw_kinds` (the ingest's setting), `calls`
    otherwise; the read/write distinction also rides the facets on the
    same statement.

Subqueries, table functions (`numbers(10)`, `remote(…)`), CTE names and
`system.*` / `information_schema.*` are not references: the first two are
syntax, the third is scoped to the statement, the last has its own facet.

Outside the extractor stamp with `lang_sql.py` (see
`tests/unit/test_code_extractor_stamp.py`): rows beside the statement
records, applied by `loom code backfill --paths-file` over the SQL tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_sql import statement_symbol
from server.code.domain.extract.lang_sql_facets import line_of_name, statement_facets
from server.code.domain.extract.lang_sql_model import FACET_FUNCTION, Facet, Statement
from server.code.domain.extract.name_scope import SQL_OBJECT_KINDS
from server.code.domain.extract.treesitter import Symbol

FACET_CREATES = "creates"
FACET_TABLE_READ = "table_read"
FACET_TABLE_WRITE = "table_write"

# CREATE word → symbol kind (ctags' SQL kinds, plus ClickHouse's)
OBJECT_KIND_BY_WORD: dict[str, str] = {
    "TABLE": "table",
    "VIEW": "view",
    "INDEX": "index",
    "FUNCTION": "function",
    "PROCEDURE": "procedure",
    "TRIGGER": "trigger",
    "TYPE": "type",
    "SCHEMA": "schema",
    "DATABASE": "database",
    "SEQUENCE": "sequence",
    "DICTIONARY": "dictionary",
    "ROLE": "role",
    "USER": "user",
    "POLICY": "policy",
    "PROFILE": "policy",
    "QUOTA": "policy",
    "EXTENSION": "extension",
    "DOMAIN": "domain",
    "AGGREGATE": "aggregate",
}
CALLABLE_KINDS: frozenset[str] = frozenset({"function", "procedure", "aggregate"})

# `name`, `"quoted name"`, `` `name` ``, `db.name`, `{CLICKHOUSE_DATABASE:Identifier}.t`
_SEG = r'(?:"[^"\n]+"|`[^`\n]+`|\[[^\[\]\n]+\]|\{[^{}\n]*\}|[A-Za-z_][\w$]*)'
_NAME = rf"({_SEG}(?:\.{_SEG}){{0,2}})"
_CREATE_RE = re.compile(
    r"(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:(?:TEMP|TEMPORARY|UNLOGGED|MATERIALIZED|UNIQUE|LIVE|WINDOW|ROW|SETTINGS|GLOBAL|LOCAL"
    r"|EXTERNAL|FOREIGN|REPLICATED|RECURSIVE|REFRESHABLE|SPGIST|GIN|GIST)\s+)*"
    r"(?P<word>TABLE|VIEW|INDEX|FUNCTION|PROCEDURE|TRIGGER|TYPE|SCHEMA|DATABASE|SEQUENCE"
    r"|DICTIONARY|ROLE|USER|POLICY|PROFILE|QUOTA|EXTENSION|DOMAIN|AGGREGATE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:CONCURRENTLY\s+)?" + _NAME
)
_READ_RE = re.compile(
    # `ARRAY JOIN col` (ClickHouse) unfolds a column, it reads no table
    r"(?i)\b(?:FROM|(?<!ARRAY )(?<!ARRAY\n)JOIN|SHOW\s+CREATE\s+(?:TABLE|VIEW|DICTIONARY)"
    r"|DESCRIBE(?:\s+TABLE)?"
    # `DESC t` only where a statement can start: `ORDER BY x DESC LIMIT 1` is not a read
    # of `LIMIT`
    r"|(?<![\w.])DESC(?:\s+TABLE)?(?=\s+[\w.]+\s*(?:;|$|FORMAT\b|SETTINGS\b))"
    r"|EXISTS\s+(?:TABLE|VIEW|DICTIONARY)|CHECK\s+TABLE|ANALYZE(?:\s+TABLE)?)\s+"
    + _NAME
    + r"(?![\w$])(?!\s*\()"
)
_COPY_TO_RE = re.compile(r"(?i)\bCOPY\s+" + _NAME + r"(?:\s*\([^()]*\))?\s+TO\b")
_WRITE_RE = re.compile(
    r"(?i)\b(?:INSERT\s+INTO(?:\s+TABLE)?|REPLACE\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO"
    r"|TRUNCATE(?:\s+TABLE)?(?:\s+IF\s+EXISTS)?|ALTER\s+(?:TABLE|VIEW)(?:\s+IF\s+EXISTS)?"
    r"|DROP\s+(?:TABLE|VIEW|DICTIONARY|INDEX|SEQUENCE|TRIGGER|FUNCTION|PROCEDURE|TYPE)"
    r"(?:\s+IF\s+EXISTS)?|OPTIMIZE\s+TABLE|ATTACH\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?"
    r"|DETACH\s+TABLE(?:\s+IF\s+EXISTS)?|RENAME\s+TABLE|EXCHANGE\s+TABLES|REFRESH\s+MATERIALIZED\s+VIEW"
    r"|VACUUM(?:\s+FULL)?|REINDEX\s+TABLE|CLUSTER)\s+" + _NAME + r"(?![\w$])"
    # no `(?!\s*\()` here: `INSERT INTO t (a, b) VALUES` is the common write shape; the
    # one table function in write position is ClickHouse's `INSERT INTO FUNCTION
    # f(...)`, excluded by name below
)
_WRITE_NOT_A_TABLE = frozenset({"function", "table"})
_COPY_FROM_RE = re.compile(r"(?i)\bCOPY\s+" + _NAME + r"(?:\s*\([^()]*\))?\s+FROM\b")
# ClickHouse's `ALTER TABLE t UPDATE col = …` — the bare `UPDATE <name>` alternative
# above would read `col` as a written table
_ALTER_UPDATE_NAME_RE = re.compile(r"(?i)\bUPDATE\s+" + _NAME)
# A comma-joined FROM list (`FROM t1, t2 AS b`) reads every table in it, not only the
# first
_FROM_LIST_RE = re.compile(
    r"(?i)\bFROM\s+" + _NAME + r"(?![\w$])(?!\s*\()"
    r"(?:\s+(?:AS\s+)?(?!(?:WHERE|GROUP|ORDER|LIMIT|HAVING|UNION|FINAL|SAMPLE|PREWHERE|SETTINGS"
    r"|FORMAT|JOIN|LEFT|RIGHT|INNER|FULL|CROSS|ON|USING|WINDOW|QUALIFY|INTO|RETURNING)\b)\w+)?"
    r"((?:\s*,\s*" + _NAME + r"(?![\w$])(?!\s*\()(?:\s+(?:AS\s+)?(?!(?:WHERE|GROUP|ORDER|LIMIT"
    r"|HAVING|UNION|FINAL|SAMPLE|PREWHERE|SETTINGS|FORMAT|JOIN|LEFT|RIGHT|INNER|FULL|CROSS|ON|USING"
    r"|WINDOW|QUALIFY|INTO|RETURNING)\b)\w+)?)+)"
)
_LIST_NAME_RE = re.compile(r",\s*" + _NAME)
_ON_TARGET_RE = re.compile(r"(?i)\bON\s+(?:ONLY\s+)?" + _NAME + r"\s*(?:\(|USING|FOR|AS|\b)")
_MV_TO_RE = re.compile(
    r"(?i)\bMATERIALIZED\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    + _NAME
    + r"(?:\s+ON\s+CLUSTER\s+\S+)?\s+TO\s+"
    + _NAME
)
_CTE_RE = re.compile(
    r"(?i)(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)(" + _SEG + r")(?:\s*\([^()]*\))?\s+AS\s*\("
)
# Function names every dialect ships: a call to one never binds to a same-
# named definition elsewhere in the tree (a Python `count`, a C++ `now`).
_BUILTIN_FUNCTIONS = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "any",
        "now",
        "today",
        "coalesce",
        "cast",
        "concat",
        "length",
        "lower",
        "upper",
        "substring",
        "substr",
        "round",
        "floor",
        "ceil",
        "abs",
        "random",
        "rand",
        "array_agg",
        "string_agg",
        "unnest",
        "generate_series",
        "nullif",
        "greatest",
        "least",
        "extract",
        "date_trunc",
        "to_char",
        "to_date",
        "to_timestamp",
        "row_number",
        "rank",
        "dense_rank",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "ntile",
        "exists",
        "in",
        "if",
        "and",
        "or",
        "not",
        "like",
        "position",
        "replace",
        "trim",
        "ltrim",
        "rtrim",
        "left",
        "right",
        "format",
        "toString",
        "toInt32",
        "toInt64",
        "toUInt32",
        "toUInt64",
        "toDate",
        "toDateTime",
        "toFloat64",
        "arrayJoin",
        "groupArray",
        "uniq",
        "uniqExact",
        "countIf",
        "sumIf",
        "argMax",
        "argMin",
        "range",
        "array",
        "tuple",
        "map",
        "number",
        "numbers",
        "sleep",
        "version",
        "currentDatabase",
        "hostName",
        "materialize",
        "ignore",
        "assumeNotNull",
        "isNull",
        "isNotNull",
        "empty",
        "notEmpty",
        "has",
        "indexOf",
        "splitByChar",
        "toYear",
        "toMonth",
        "toStartOfDay",
        "toStartOfMonth",
        "dateDiff",
        "addDays",
        "subtractDays",
        "json_build_object",
        "jsonb_build_object",
        "json_agg",
        "jsonb_agg",
        "md5",
        "sha256",
        "current_timestamp",
        "current_date",
        "localtimestamp",
        "pg_sleep",
        "setval",
        "nextval",
        "currval",
        "lastval",
        "quote_ident",
        "quote_literal",
        "regexp_replace",
        "regexp_matches",
        "split_part",
        "array_length",
        "cardinality",
        "date_part",
        "age",
        "date",
        "time",
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "second",
        "ifnull",
        "isnull",
        "char_length",
        "group_concat",
        "convert",
        "getdate",
        "datediff",
        "dateadd",
        "len",
        "isnumeric",
    }
)
_INFO_SCHEMA_PREFIXES = ("system.", "information_schema.", "pg_catalog.", "mysql.", "sys.")
_NOT_A_TABLE = frozenset(
    {
        "SELECT",
        "VALUES",
        "DUAL",
        "LATERAL",
        "UNNEST",
        "ONLY",
        "GENERATE_SERIES",
        "INPUT",
        "FILE",
        "URL",
        "S3",
        "MERGE",
        "VIEW",
        "TABLE",
        "FUNCTION",
        "EXISTS",
        "WHERE",
        "SET",
        "ON",
        "AS",
        "INTO",
        "DELETE",
        "UPDATE",
        "FORMAT",
        "SETTINGS",
        "CLUSTER",
        "REMOTE",
        "NUMBERS",
        "ZEROS",
        "STDIN",
        "STDOUT",
        "PROGRAM",
        "DATABASE",
        "DICTIONARY",
        "IF",
        "NOT",
        "TEMPORARY",
        "TEMP",
        "CURRENT_DATABASE",
        "CURRENT_USER",
        "DEFAULT",
        "FINAL",
        "SAMPLE",
        "PREWHERE",
        "ARRAY",
        "INFORMATION_SCHEMA",
        "SYSTEM",
        "TO",
        "WITH",
        "ALL",
        "GLOBAL",
        "ANY",
        "LEFT",
        "RIGHT",
        "INNER",
        "FULL",
        "CROSS",
        "ASOF",
        "SEMI",
        "ANTI",
        "PASTE",
        "USING",
        "DISTINCT",
        "OUTER",
    }
)


def bare_name(name: str) -> str:
    """`db."Users"` → `Users`; `{CLICKHOUSE_DATABASE:Identifier}.t` → `t`."""
    seg = name.rsplit(".", 1)[-1].strip()
    if seg[:1] in '"`[' and len(seg) >= 2:
        seg = seg[1:-1]
    return seg


def _plain(name: str) -> str:
    return name.replace('"', "").replace("`", "").replace("[", "").replace("]", "")


def _is_table_ref(name: str, ctes: set[str]) -> bool:
    plain = _plain(name)
    if plain.lower().startswith(_INFO_SCHEMA_PREFIXES):
        return False
    if plain.startswith("{") and "." not in plain:
        return False  # a bare `{macro}` names nothing the tree holds
    bare = bare_name(name)
    return bool(bare) and bare.upper() not in _NOT_A_TABLE and bare not in ctes


@dataclass(frozen=True)
class CreatedObject:
    kind: str  # symbol kind: table | view | …
    name: str  # as written (`db.t`, `"Users"`)
    bare: str
    schema: str = ""  # the qualifier as written, or the file's USE / search_path context

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.bare}" if self.schema else self.bare


def split_schema(name: str) -> tuple[str, str]:
    """(`schema`, `bare`) of a written name: `public.users` → (public,
    users); `db.schema.t` → (db.schema, t); a `{macro}` qualifier (ClickHouse's
    `{CLICKHOUSE_DATABASE:Identifier}.t`) is the current database, so ''."""
    plain = _plain(name)
    if "." not in plain:
        return "", plain
    schema, bare = plain.rsplit(".", 1)
    if schema.startswith("{"):
        return "", bare
    return schema, bare


_USE_RE = re.compile(r"(?is)^\s*USE\s+(?:DATABASE\s+|SCHEMA\s+)?" + _SEG)
_SEARCH_PATH_RE = re.compile(
    r"(?is)^\s*SET\s+(?:LOCAL\s+|SESSION\s+)?search_path\s*(?:=|TO)\s*([^;,]+)"
)
_SET_SCHEMA_RE = re.compile(r"(?is)^\s*SET\s+SCHEMA\s+" + _SEG)
_ALTER_SESSION_SCHEMA_RE = re.compile(
    r"(?is)^\s*ALTER\s+SESSION\s+SET\s+CURRENT_SCHEMA\s*=\s*" + _SEG
)


def schema_contexts(statements: list[Statement]) -> dict[int, str]:
    """{statement ordinal: the schema unqualified names live in from that
    statement on}, from `USE x`, `SET search_path TO x, y` (first entry, as
    PostgreSQL resolves), `SET SCHEMA x`, `ALTER SESSION SET CURRENT_SCHEMA`.
    '' until one is seen."""
    out: dict[int, str] = {}
    current = ""
    for st in statements:
        body = st.body
        m = (
            _USE_RE.match(body)
            or _SET_SCHEMA_RE.match(body)
            or _ALTER_SESSION_SCHEMA_RE.match(body)
        )
        if m:
            current = _plain(m.group(0).split()[-1]).strip()
        else:
            sp = _SEARCH_PATH_RE.match(body)
            if sp:
                first = sp.group(1).split(",")[0].strip().strip("'\"")
                current = "" if first.lower() in ("public", '"$user"', "$user") else first
        out[st.ordinal] = current
    return out


@dataclass
class References:
    creates: CreatedObject | None = None
    reads: list[str] = field(default_factory=list)  # as written, deduplicated, in order
    writes: list[str] = field(default_factory=list)


def created_object(stmt: Statement, context_schema: str = "") -> CreatedObject | None:
    m = _CREATE_RE.match(stmt.body)
    if not m:
        return None
    kind = OBJECT_KIND_BY_WORD[m.group("word").upper()]
    name = m.group(2)
    schema, bare = split_schema(name)
    if kind in ("schema", "database"):
        context_schema = schema = ""  # a schema is not inside a schema
    return CreatedObject(
        kind,
        _plain(name) if not name.startswith(('"', "`")) else name,
        bare,
        schema or context_schema,
    )


def references(stmt: Statement) -> References:
    """What one statement defines, reads and writes."""
    body = stmt.body
    ctes = {bare_name(m) for m in _CTE_RE.findall(body)}
    refs = References(creates=created_object(stmt))
    seen: set[str] = set()

    def add(bucket: list[str], name: str) -> None:
        plain = _plain(name)
        if _is_table_ref(name, ctes) and plain not in seen:
            seen.add(plain)
            bucket.append(plain)

    alter_update = (
        {_plain(n) for n in _ALTER_UPDATE_NAME_RE.findall(body)}
        if stmt.kind.upper().startswith("ALTER")
        else set()
    )
    for name in _WRITE_RE.findall(body):
        if name.lower() not in _WRITE_NOT_A_TABLE and _plain(name) not in alter_update:
            add(refs.writes, name)
    for name in _COPY_FROM_RE.findall(body):
        add(refs.writes, name)
    mv = _MV_TO_RE.search(body)
    if mv:
        add(refs.writes, mv.group(2))
    if refs.creates and refs.creates.kind in ("index", "trigger", "policy"):
        on = _ON_TARGET_RE.search(body)
        if on:
            add(refs.writes, on.group(1))
    for name in _READ_RE.findall(body):
        add(refs.reads, name)
    for m in _FROM_LIST_RE.finditer(body):
        for name in _LIST_NAME_RE.findall(m.group(2)):
            add(refs.reads, name)
    for name in _COPY_TO_RE.findall(body):
        add(refs.reads, name)
    if refs.creates:
        # the object being defined is not a reference to itself
        own = _plain(refs.creates.name)
        refs.reads = [r for r in refs.reads if r != own]
        refs.writes = [w for w in refs.writes if w != own]
    return refs


def object_symbols(path: str, mod: str, statements: list[Statement]) -> list[Symbol]:
    """One symbol per object the file creates — `<module>::<schema.name>` when the name
    is written qualified or a `USE` / `search_path` is in force, `<module>::<name>`
    otherwise; the first creation wins when a test creates the same name twice (DROP /
    CREATE patterns)."""
    ctx = schema_contexts(statements)
    out: dict[tuple[str, str, str], Symbol] = {}
    for st in statements:
        obj = created_object(st, ctx.get(st.ordinal, ""))
        if obj is None:
            continue
        key = (obj.kind, obj.schema, obj.bare)
        if key in out:
            continue
        out[key] = Symbol(
            qualified_name=f"{mod}::{obj.qualified}",
            name=obj.bare,
            kind=obj.kind,
            signature=f"{obj.kind} {obj.name}",
            path=path,
            start_line=st.start_line,
            end_line=st.end_line,
            doc_comment=st.comments,
        )
    return list(out.values())


def reference_facets(statements: list[Statement]) -> list[Facet]:
    out: list[Facet] = []
    for st in statements:
        refs = references(st)
        if refs.creates:
            out.append(Facet(FACET_CREATES, refs.creates.bare, st.ordinal, st.start_line, 1.0))
        for name in refs.reads:
            out.append(
                Facet(
                    FACET_TABLE_READ,
                    bare_name(name),
                    st.ordinal,
                    line_of_name(st, bare_name(name)),
                    1.0,
                )
            )
        for name in refs.writes:
            out.append(
                Facet(
                    FACET_TABLE_WRITE,
                    bare_name(name),
                    st.ordinal,
                    line_of_name(st, bare_name(name)),
                    1.0,
                )
            )
    return out


def reference_names(statements: list[Statement]) -> set[str]:
    """The bare names a file's statements reference — tables and the
    functions they call — for the name-table prefetch."""
    names: set[str] = set()
    for st in statements:
        refs = references(st)
        names.update(bare_name(n) for n in refs.reads)
        names.update(bare_name(n) for n in refs.writes)
        names.update(f.name for f in statement_facets(st) if f.kind == FACET_FUNCTION)
    return names


EDGE_READS = "reads"
EDGE_WRITES = "writes"


def _candidate_scope(qualified_name: str) -> tuple[tuple[str, ...], str]:
    """(directory components, schema) of a candidate's qualified name:
    `examples.authors.postgresql.schema::public.authors` →
    (('examples','authors','postgresql'), 'public')."""
    mod, _, tail = qualified_name.partition("::")
    parts = tuple(mod.split(".")[:-1]) if mod else ()
    schema = tail.rsplit(".", 1)[0] if "." in tail else ""
    return parts, schema


def _common(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def resolve_object(
    bare: str,
    schema: str,
    kinds: frozenset[str],
    own: list[Symbol],
    name_table: dict[str, list[tuple[str, str, str]]],
    path: str,
) -> tuple[str, str, float] | None:
    """(symbol_id, qualified_name, confidence) for a reference, by the ladder: the
    file's own definition (1.0; the same schema first when the reference is qualified);
    then, when the reference is qualified, the candidates written in that schema; then
    the nearest definition by directory — the candidate whose module shares the longest
    directory prefix with the referencing file, when exactly one definition stands at
    that depth (0.95; sqlc's `examples/*/schema.sql`, sakila's per-dialect folders);
    then a repository-unique definition (0.9). Several definitions at the winning rung
    bind nothing."""
    local = [s for s in own if s.name == bare and s.kind in kinds]
    if schema:
        same = [s for s in local if _candidate_scope(s.qualified_name)[1] == schema]
        local = same or local
    if local:
        return local[0].id, local[0].qualified_name, 1.0
    cands = [(sid, qn) for sid, qn, k in name_table.get(bare, []) if k in kinds]
    if schema:
        in_schema = [(sid, qn) for sid, qn in cands if _candidate_scope(qn)[1] == schema]
        cands = in_schema or cands
    if not cands:
        return None
    here = tuple(module_scope_dir(path))
    scored = sorted(
        ((_common(_candidate_scope(qn)[0], here), sid, qn) for sid, qn in cands),
        key=lambda t: (-t[0], t[2], t[1]),
    )
    best = scored[0][0]
    at_best = sorted({(qn, sid) for sc, sid, qn in scored if sc == best})
    if len({qn for qn, _sid in at_best}) != 1:
        # Several definitions at the winning depth bind only when they sit in ONE
        # directory — a migration folder that creates, drops and recreates `users`,
        # sakila's schema then its `-data` file — and then the newest by path order
        # (`0002_…` after `0001_…`, a timestamp after an earlier one) is the live shape
        dirs = {_candidate_scope(qn)[0] for qn, _sid in at_best}
        schemas = {_candidate_scope(qn)[1] for qn, _sid in at_best}
        if len(dirs) != 1 or len(schemas) != 1:
            return None  # two schemas' `users` in one folder are two objects
        qn, sid = max(at_best, key=lambda t: t[0].partition("::")[0])
        return sid, qn, 0.8
    qn, sid = at_best[0]
    return sid, qn, 0.95 if best > 0 else 0.9


def module_scope_dir(path: str) -> list[str]:
    return path.split("/")[:-1]


def _unique(
    candidates: list[tuple[str, str, str]], kinds: frozenset[str]
) -> tuple[str, str] | None:
    """(symbol_id, qualified_name) when every candidate of an accepted kind
    names the same definition; None when nobody or several do."""
    kept = {(sid, qn) for sid, qn, kind in candidates if kind in kinds}
    if len({qn for _, qn in kept}) != 1:
        return None
    return sorted(kept)[0]


def sql_edges(
    path: str,
    mod: str,
    statements: list[Statement],
    own: list[Symbol],
    name_table: dict[str, list[tuple[str, str, str]]],
    *,
    rw_kinds: bool = False,
) -> list[Edge]:
    """Statement → object edges, resolved by `resolve_object`'s ladder (own file,
    written schema, nearest directory, repository-unique); a function call resolves to a
    SQL function / procedure the same way. Nothing is recorded as a miss — a table
    nobody creates in the tree is the norm for a query file against a live database.
    With `rw_kinds` a table reference is a `writes` or `reads` edge;
    without it every edge is `calls`. Each edge sits on the line the reference is
    written on (a body's inner statement, not the CREATE)."""
    edges: list[Edge] = []
    # `resolve_object`'s first own definition per name, and per name in a schema, found
    # once: a scan of `own` per reference costs quadratic time
    first: dict[tuple[frozenset[str], str, str], Symbol] = {}
    for family in (SQL_OBJECT_KINDS, CALLABLE_KINDS):
        for sym in own:
            if sym.kind in family:
                first.setdefault((family, sym.name, ""), sym)
                first.setdefault((family, sym.name, _candidate_scope(sym.qualified_name)[1]), sym)
    for st in statements:
        src = statement_symbol(path, mod, st)
        refs = references(st)
        targets: list[tuple[str, str, frozenset[str], str]] = [
            *(
                (*split_schema(n), SQL_OBJECT_KINDS, EDGE_WRITES if rw_kinds else "calls")
                for n in refs.writes
            ),
            *(
                (*split_schema(n), SQL_OBJECT_KINDS, EDGE_READS if rw_kinds else "calls")
                for n in refs.reads
            ),
            *(
                ("", f.name, CALLABLE_KINDS, "calls")
                for f in statement_facets(st)
                if f.kind == FACET_FUNCTION
            ),
        ]
        seen: set[tuple[str, str]] = set()
        for schema, bare, kinds, edge_kind in targets:
            if (bare, edge_kind) in seen:
                continue
            seen.add((bare, edge_kind))
            if (
                kinds is CALLABLE_KINDS
                and bare in _BUILTIN_FUNCTIONS
                and (kinds, bare, "") not in first
            ):
                continue  # `count()` binds to nobody's definition
            mine = first.get((kinds, bare, schema)) or first.get((kinds, bare, ""))
            hit = resolve_object(bare, schema, kinds, [mine] if mine else [], name_table, path)
            if hit is None:
                continue
            to_id, to_qn, conf = hit
            if to_id == src.id:
                continue
            edges.append(
                Edge(
                    edge_kind=edge_kind,
                    from_symbol_id=src.id,
                    to_symbol_id=to_id,
                    from_qualified_name=src.qualified_name,
                    to_qualified_name=to_qn,
                    path=path,
                    line=line_of_name(st, bare),
                    confidence=conf,
                )
            )
    return edges


def prefix_parts(group: list[Statement]) -> list[str]:
    """`creates: t · reads: a, b · writes: c` for a chunk's context line."""
    creates: list[str] = []
    reads: list[str] = []
    writes: list[str] = []
    for st in group:
        refs = references(st)
        if refs.creates and refs.creates.bare not in creates:
            creates.append(refs.creates.bare)
        for n in refs.reads:
            if bare_name(n) not in reads:
                reads.append(bare_name(n))
        for n in refs.writes:
            if bare_name(n) not in writes:
                writes.append(bare_name(n))
    parts: list[str] = []
    for label, names in (("creates", creates), ("reads", reads), ("writes", writes)):
        if names:
            parts.append(f"{label}: " + ", ".join(names[:8]))
    return parts


__all__ = [
    "CALLABLE_KINDS",
    "EDGE_READS",
    "EDGE_WRITES",
    "FACET_CREATES",
    "FACET_TABLE_READ",
    "FACET_TABLE_WRITE",
    "resolve_object",
    "schema_contexts",
    "split_schema",
    "OBJECT_KIND_BY_WORD",
    "CreatedObject",
    "References",
    "bare_name",
    "created_object",
    "object_symbols",
    "prefix_parts",
    "reference_facets",
    "reference_names",
    "references",
    "sql_edges",
]
