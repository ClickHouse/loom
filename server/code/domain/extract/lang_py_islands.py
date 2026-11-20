"""SQL inside Python — the strings a database client runs.

ClickHouse's integration tests hold thousands of SQL-shaped string literals, most of
them the argument of `node.query("…")`; every application repository has its
`cursor.execute("SELECT …")`, `client.command (…)`, `session.execute(text(…))`. Without
this module the SQL a Python file runs is only words inside a chunk. It lifts those
strings through the island contract the shell lane uses:

  * **which strings**: the argument of a call whose method is in the client
    table (`PY_CLIENT_METHODS`: ClickHouse's test harness `query`,
    `query_and_get_error`, `query_with_retry`, `http_query`, `azure_query`,
    DB-API `execute` / `executemany`, clickhouse-connect `command`,
    SQLAlchemy `text`, Django `raw`, …) — positional or `sql=` / `query=`
    keyword, through `.format(...)`, `%` formatting and `+` concatenation —
    plus any other literal whose head is an upper-case SQL verb followed by
    a second SQL keyword (`sql = "SELECT … FROM …"`, run later). Docstrings
    never count.
  * **what a hole is**: an f-string `{expr}`, a `.format` `{name}`, a
    `%s` / `%(x)s`, a `?` placeholder become `__var__` (the island
    placeholder, which no facet keeps); the island is marked interpolated
    (facets at confidence 0.7).
  * **what comes out**: the strings split into statement records anchored
    to the file's lines (`island_statements`), one `statement` symbol each,
    facets (`island_facets`: statement kind, functions, settings, engines,
    dialect), the created objects as symbols and the table references as
    facets, so `code.tests_for({facets:[{kind:"engine",
    name:"Kafka"}]})` reaches integration tests, and the SQL edge stage
    binds the statements to the tables the file itself creates.

The dialect is the client's when the client table names one (ClickHouse's harness
methods), else the dialect text cue over the joined islands, else the client table's
default (`clickhouse` for `query`-shaped names in a ClickHouse tree is not assumed — an
`execute` on an unknown client stays undecided). Outside the extractor stamp like
`lang_shell_islands`; a namespace gains the rows by `loom code backfill --paths-file`
over its Python tree.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_shell_islands import (
    PLACEHOLDER,
    Island,
    island_facets,
    island_statements,
)
from server.code.domain.extract.lang_sql import statement_symbol
from server.code.domain.extract.lang_sql_columns import (
    column_facets,
    column_lineage_on,
    column_symbols,
    file_columns,
)
from server.code.domain.extract.lang_sql_dialect import detect_dialect
from server.code.domain.extract.lang_sql_model import Facet, Header, Statement
from server.code.domain.extract.lang_sql_objects import object_symbols, reference_facets
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text
from server.core.sql_comments import LEADING_COMMENTS

# method name (the last attribute segment of the callee) → dialect, or '' when
# the client is generic and the text decides
PY_CLIENT_METHODS: dict[str, str] = {
    # ClickHouse's integration test harness (tests/integration/helpers/cluster.py)
    "query": "clickhouse",
    "query_and_get_error": "clickhouse",
    "query_and_get_answer_with_error": "clickhouse",
    "query_with_retry": "clickhouse",
    "http_query": "clickhouse",
    "http_query_and_get_error": "clickhouse",
    "azure_query": "clickhouse",
    "query_and_get_error_with_retry": "clickhouse",
    # clickhouse-connect / clickhouse-driver
    "command": "clickhouse",
    "query_df": "clickhouse",
    "query_np": "clickhouse",
    "query_arrow": "clickhouse",
    "query_row_block_stream": "clickhouse",
    "execute_iter": "clickhouse",
    "insert": "",
    # DB-API and ORMs: the text decides
    "execute": "",
    "executemany": "",
    "executescript": "",
    "exec_driver_sql": "",
    "text": "",
    "raw": "",
    "run_query": "",
    "run_sql": "",
    "exec_query": "",
    "sql": "",
    "read_sql": "",
    "read_sql_query": "",
    "fetch": "",
    "fetchrow": "",
    "fetchval": "",
    "q": "clickhouse",
    # ClickHouse's Iceberg / Delta test helpers submit Spark SQL
    "execute_spark_query": "sparksql",
    "execute_spark_query_general": "sparksql",
}
# the object a client method is called on, when its name settles the
# dialect: Spark's `spark.sql(...)` in the Iceberg / Delta tests speaks
# Spark SQL, whose `ARRAY<STRUCT<…>>` reads as BigQuery to the text rule
PY_CLIENT_OBJECTS: dict[str, str] = {"spark": "sparksql", "spark_session": "sparksql"}
# a keyword argument that carries the SQL when it is not positional
_SQL_KWARGS = frozenset({"sql", "query", "statement", "q", "text"})
_SQL_HEAD_RE = re.compile(
    r"^\s*(SELECT|INSERT|CREATE|DROP|ALTER|WITH|UPDATE|DELETE|ATTACH|DETACH|EXPLAIN|SHOW"
    r"|DESCRIBE|DESC|OPTIMIZE|SYSTEM|TRUNCATE|GRANT|REVOKE|RENAME|EXCHANGE|KILL|SET|USE|CHECK"
    r"|EXISTS|BACKUP|RESTORE|MERGE|COPY|VACUUM|ANALYZE|BEGIN|COMMIT|ROLLBACK)\b"
)
_SECOND_KEYWORD_RE = re.compile(
    r"\b(FROM|TABLE|INTO|WHERE|VALUES|SET|DATABASE|VIEW|DICTIONARY|USER|ROLE|QUOTA|POLICY"
    r"|FUNCTION|INDEX|SETTINGS|FORMAT|JOIN|GROUP|ORDER|LIMIT|PARTITION|FLUSH|RELOAD|STOP|START"
    r"|SYNC|MERGES|PROFILE|GRANT|ON|TO|IF|EXISTS|CLUSTER|TABLES|DATABASES|PROCESSLIST)\b"
)
_PCT_RE = re.compile(r"%\((\w+)\)[sdifr]|%[sdifr]|\?(?=[\s,)]|$)")
_BRACE_RE = re.compile(r"#?\{[^{}]*\}")  # `.format` braces; MyBatis `#{id}` too

CONF_INTERPOLATED = 0.7
_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\\\": "\\", "\\'": "'", '\\"': '"'}


@dataclass
class PySqlUnits:
    islands: list[Island] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    facets: list[Facet] = field(default_factory=list)


def _callee(call: Any, source: bytes) -> tuple[str, str]:
    """(method name, object name) of the callee: `node.query` → ("query",
    "node"), `spark.sql` → ("sql", "spark"), `text(...)` → ("text", "")."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return "", ""
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        obj = fn.child_by_field_name("object")
        obj_name = (
            node_text(obj, source).rsplit(".", 1)[-1]
            if obj is not None and obj.type in ("identifier", "attribute")
            else ""
        )
        return (node_text(attr, source) if attr is not None else ""), obj_name
    if fn.type == "identifier":
        return node_text(fn, source), ""
    return "", ""


def _string_value(node: Any, source: bytes) -> tuple[str, bool]:
    """The literal's text with interpolations as the placeholder;
    (value, interpolated). Handles plain, f- and implicitly concatenated
    strings; `.format` / `%` holes are substituted by the caller."""
    if node.type == "concatenated_string":
        parts = [_string_value(c, source) for c in node.named_children if c.type == "string"]
        return "".join(p[0] for p in parts), any(p[1] for p in parts)
    if node.type != "string":
        return "", False
    out: list[str] = []
    interpolated = False
    for c in node.children:
        if c.type == "string_content":
            out.append(node_text(c, source))
        elif c.type == "interpolation":
            out.append(PLACEHOLDER)
            interpolated = True
        elif c.type == "escape_sequence":
            raw = node_text(c, source)
            out.append(_ESCAPES.get(raw, raw))
    return "".join(out), interpolated


def _holes(text: str) -> tuple[str, bool]:
    """`.format` braces, `%s` / `%(x)s`, `?` → placeholder."""
    out, n1 = _BRACE_RE.subn(PLACEHOLDER, text)
    out, n2 = _PCT_RE.subn(PLACEHOLDER, out)
    return out, bool(n1 or n2)


_LEADING_COMMENTS_RE = re.compile(LEADING_COMMENTS)


def _sql_head(text: str) -> str:
    """The text after any leading SQL comments (sqlc's `-- name: X :one`
    line heads every generated query constant)."""
    return _LEADING_COMMENTS_RE.sub("", text, count=1)


def _looks_like_sql(text: str) -> bool:
    """The literal lane's rule: an upper-case SQL verb, then a second SQL
    keyword — `"Create a table from Arrow data."` and `"CREATE"` are prose."""
    head = _sql_head(text)
    m = _SQL_HEAD_RE.match(head)
    if not m or not m.group(1).isupper():
        return False
    return bool(_SECOND_KEYWORD_RE.search(head[m.end() :]))


_HEAD_ANY_CASE_RE = re.compile(_SQL_HEAD_RE.pattern, re.IGNORECASE)


def _client_arg_is_sql(text: str) -> bool:
    """The sink lane's looser rule: a client runs whatever it is handed, but
    `command` is also pymongo's (`db.command("createUser", …)`) — the text
    must open with a SQL verb (any case), with a hole (`f"{query} SETTINGS
    …"`), or carry a SQL keyword somewhere."""
    head = _sql_head(text)
    if not head:
        return False
    if _HEAD_ANY_CASE_RE.match(head) or head.startswith(PLACEHOLDER):
        return True
    return bool(_SECOND_KEYWORD_RE.search(head))


def _is_docstring(node: Any) -> bool:
    """A string that is the whole first statement of a module / def / class body."""
    p = node.parent
    if p is None or p.type != "expression_statement":
        return False
    body = p.parent
    if body is None or body.type not in ("module", "block"):
        return False
    first = next((c for c in body.named_children if c.type != "comment"), None)
    return first is p


def _string_nodes_in(arg: Any) -> list[Any]:
    """The string literal(s) an argument carries: the literal itself, the
    object of `"…".format(…)`, the left side of `"…" % x`, the operands of
    `"…" + y` in order — a non-string operand of `+` is a hole (None)."""
    if arg.type in ("string", "concatenated_string"):
        return [arg]
    if arg.type == "call":
        fn = arg.child_by_field_name("function")
        if fn is not None and fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            obj = fn.child_by_field_name("object")
            if attr is not None and obj is not None and attr.text == b"format":
                return _string_nodes_in(obj)
        return []
    if arg.type == "binary_operator":
        left = arg.child_by_field_name("left")
        right = arg.child_by_field_name("right")
        op = next((c for c in arg.children if c.type in ("%", "+")), None)
        if op is None or left is None or right is None:
            return []
        if op.type == "%":
            return _string_nodes_in(left)
        l_nodes, r_nodes = _string_nodes_in(left), _string_nodes_in(right)
        if not l_nodes and not r_nodes:
            return []
        return [*(l_nodes or [None]), *(r_nodes or [None])]
    if arg.type == "parenthesized_expression":
        inner = next((c for c in arg.named_children), None)
        return _string_nodes_in(inner) if inner is not None else []
    return []


def _island_from(
    nodes: list[Any], source: bytes, dialect: str, via: str, formatted: bool
) -> Island | None:
    real = [n for n in nodes if n is not None]
    if not real:
        return None
    parts = [_string_value(n, source) if n is not None else (PLACEHOLDER, True) for n in nodes]
    text = "".join(p[0] for p in parts)
    interpolated = any(p[1] for p in parts)
    text, holed = _holes(text)
    if not text.strip():
        return None
    return Island(
        text, real[0].start_point[0] + 1, via, interpolated or holed or formatted, (), dialect
    )


def find_py_islands(pf: ParsedFile) -> list[Island]:
    """SQL islands of a parsed Python file, in source order."""
    if pf.tree is None:
        return []
    source = pf.source
    out: list[Island] = []
    claimed: set[int] = set()  # start bytes of literals already lifted by a call

    def visit(n: Any) -> None:
        if n.type == "call":
            name, obj = _callee(n, source)
            if name in PY_CLIENT_METHODS:
                dialect = PY_CLIENT_OBJECTS.get(obj) or PY_CLIENT_METHODS[name]
                args = n.child_by_field_name("arguments")
                cands: list[Any] = []
                if args is not None:
                    for a in args.named_children:
                        if a.type == "keyword_argument":
                            k = a.child_by_field_name("name")
                            v = a.child_by_field_name("value")
                            is_sql_kwarg = k is not None and node_text(k, source) in _SQL_KWARGS
                            if is_sql_kwarg and v is not None:
                                cands.append(v)
                        elif a.type not in ("comment",):
                            cands.append(a)
                            break  # the first positional argument is the SQL
                for cand in cands:
                    nodes = _string_nodes_in(cand)
                    formatted = cand.type in ("call", "binary_operator")
                    isl = _island_from(nodes, source, dialect, "py_call", formatted)
                    if isl is not None and _client_arg_is_sql(isl.text):
                        claimed.update(x.start_byte for x in nodes if x is not None)
                        out.append(isl)
        for c in n.children:
            visit(c)

    visit(pf.tree.root_node)
    out.extend(_assembled(pf, claimed))
    out.extend(_joined(pf, claimed))

    def visit_literals(n: Any) -> None:
        if n.type == "string" and n.start_byte not in claimed and not _is_docstring(n):
            value, interpolated = _string_value(n, source)
            if _looks_like_sql(value):
                text, holed = _holes(value)
                out.append(
                    Island(text, n.start_point[0] + 1, "py_literal", interpolated or holed, (), "")
                )
            return
        for c in n.children:
            visit_literals(c)

    visit_literals(pf.tree.root_node)
    out.sort(key=lambda i: i.line)
    return out


def _joined(pf: ParsedFile, claimed: set[int]) -> list[Island]:
    """`" ".join(["SELECT a", "FROM t", …])` is one island — the list's string elements
    joined by the separator, a non-string element a hole; only when two or more pieces
    read as SQL together."""
    if pf.tree is None:
        return []
    source = pf.source
    out: list[Island] = []

    def visit(n: Any) -> None:
        if n.type == "call":
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            if (
                fn is not None
                and fn.type == "attribute"
                and args is not None
                and fn.child_by_field_name("attribute") is not None
                and node_text(fn.child_by_field_name("attribute"), source) == "join"
            ):
                sep_node = fn.child_by_field_name("object")
                seq = next((a for a in args.named_children if a.type in ("list", "tuple")), None)
                if sep_node is not None and sep_node.type == "string" and seq is not None:
                    sep = _string_value(sep_node, source)[0]
                    pieces: list[str] = []
                    nodes: list[Any] = []
                    holed = False
                    for e in seq.named_children:
                        if e.type in ("string", "concatenated_string"):
                            v, interp = _string_value(e, source)
                            pieces.append(v)
                            nodes.append(e)
                            holed = holed or interp
                        else:
                            pieces.append(PLACEHOLDER)
                            holed = True
                    if len(nodes) >= 2 and not any(x.start_byte in claimed for x in nodes):
                        text, h2 = _holes(sep.join(pieces))
                        if _looks_like_sql(text):
                            claimed.update(x.start_byte for x in nodes)
                            out.append(
                                Island(
                                    text,
                                    nodes[0].start_point[0] + 1,
                                    "assembled",
                                    holed or h2,
                                    (),
                                    "",
                                )
                            )
                            return
        for c in n.children:
            visit(c)

    visit(pf.tree.root_node)
    return out


def _assembled(pf: ParsedFile, claimed: set[int]) -> list[Island]:
    """`q = "SELECT a"; q += " FROM t"; q = q + " WHERE …"` is one island — the
    grammar-as-data walker of the code islands with Python's assignment and `+` nodes (a
    lazy import: that module reuses this one's guards)."""
    from server.code.domain.extract.lang_code_islands import IslandGrammar, assembled_islands

    def callee(call: Any, source: bytes) -> tuple[str, str]:
        return _callee(call, source)

    grammar = IslandGrammar(
        lang="python",
        call_node="call",
        string_nodes=frozenset({"string", "concatenated_string"}),
        methods=PY_CLIENT_METHODS,
        callee=callee,
        string_value=_string_value,
        concat_node="binary_operator",
        assign_nodes=(("assignment", "left", "right"),),
        aug_assign_node=("augmented_assignment", "left", "operator", "right"),
    )
    return assembled_islands(pf, grammar, claimed)


def _settle_dialects(islands: list[Island]) -> list[Island]:
    """An island whose client named no dialect takes the dialect cue's verdict over its
    own text when that is clear or leans; else the one dialect the file's clients name
    (a `QUERY = "…"` literal next to `node.query` calls is ClickHouse); else the text
    verdict over the file's undecided SQL as a whole; else stays undecided ('')."""
    if all(i.dialect for i in islands):
        return islands
    named = Counter(i.dialect for i in islands if i.dialect)
    if len(named) == 1:
        file_guess = next(iter(named))
    elif named:
        # a file that speaks two dialects (ClickHouse and Spark SQL in the
        # Iceberg tests): an undecided literal follows the majority client
        file_guess = named.most_common(1)[0][0]
    else:
        file_guess = detect_dialect("\n".join(i.text for i in islands))[0]
    out: list[Island] = []
    for i in islands:
        d = i.dialect or detect_dialect(i.text)[0] or file_guess
        out.append(Island(i.text, i.line, i.via, i.interpolated, i.settings, d))
    return out


def py_sql_units(pf: ParsedFile) -> PySqlUnits:
    """Statements, symbols (statement records + created objects) and facets
    for the SQL a Python file runs; empty when it runs none."""
    islands = _settle_dialects(find_py_islands(pf))
    units = PySqlUnits(islands=islands)
    if not islands:
        return units
    pairs = island_statements(islands)
    statements = [st for st, _ in pairs]
    mod = module_scope(pf.path)
    units.statements = statements
    units.symbols = [statement_symbol(pf.path, mod, st) for st in statements]
    units.symbols.extend(object_symbols(pf.path, mod, statements))
    # a created object or a reference whose name is a hole (`CREATE TABLE
    # {db}.t`, `INSERT INTO {table}`) names nothing: no symbol, no facet
    units.symbols = [s for s in units.symbols if PLACEHOLDER not in s.qualified_name]
    facets = island_facets(Header(), pairs, islands) + reference_facets(statements)
    if column_lineage_on():  # the columns the test's DDL defines
        units.symbols.extend(column_symbols(pf.path, mod, statements))
        facets = facets + column_facets(statements, file_columns(statements))
    # an undecided client leaves no dialect facet (island_facets writes ''
    # otherwise)
    units.facets = [f for f in facets if f.name and PLACEHOLDER not in f.name]
    return units


__all__ = [
    "CONF_INTERPOLATED",
    "PY_CLIENT_METHODS",
    "PY_CLIENT_OBJECTS",
    "PySqlUnits",
    "find_py_islands",
    "py_sql_units",
]
