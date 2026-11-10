r"""SQL inside TypeScript template literals.

A service written against ClickHouse or Postgres keeps most of its SQL in the
application language: langfuse's `packages/shared/src/server/ repositories/*.ts` hold
373 `queryClickhouse({ query: \`…\` })` calls, 66 `commandClickhouse`, 147 `const query
= \`SELECT …\`` literals — the reads and writes of `observations`, `traces`, `scores`,
`events_core` that the ClickHouse migrations under `clickhouse/migrations/canonical/`
create. Without this module those literals are opaque text inside a function chunk.

This module is the TypeScript row of the island contract that `lang_shell_islands`
(shell) and `lang_hosts` (host languages) established, reused as data rather than
rebuilt:

  1. **Find the islands**: every `template_string` whose text starts like
     a statement (`lang_shell_islands`' head rule); `${…}` substitutions
     become `__var__` placeholders with their newlines kept, so every
     statement keeps the file line it starts on.
  2. **Read the dialect the way the file says it**: the call the literal
     sits in (`queryClickhouse`, `clickhouseClient().query`, `$queryRaw`,
     a `sql` tag — `TS_SQL_CLIENTS`, the client table's TypeScript rows),
     else `lang_sql_dialect`'s text cues, else what the file imports
     (`./clickhouse`, `@clickhouse/client` → clickhouse; `prisma`, `pg` →
     postgres).
  3. **Statement records anchored in the enclosing function**: one
     `statement` symbol per island statement under the function that
     holds it (`repositories.scores::getScoreById::stmt_0001`), facets as
     for a `.sql` file (`statement_kind`, `table_read` / `table_write`,
     `function`, `setting`, `dialect`; confidence floored at
     `CONF_INTERPOLATED` when the statement carried a placeholder), and
     the deterministic context prefix on the chunk that holds the island
     (`creates / reads / writes` from `lang_sql_objects.prefix_parts`).
  4. **Edges to the tables the migrations create**:
     `lang_sql_objects.sql_edges` over the island statements against the SQL-gated name
     table (`reads` / `writes`), from the statement AND from the enclosing function — so
     `code.callers(observations)` lists `getObservationById`, not only a
     `stmt_0003`.

Outside the extractor stamp (see `tests/unit/test_code_extractor_stamp.py`):
dispatched from `ingest.py` for `typescript` / `javascript` files; a namespace's
TypeScript tree gains the rows by a targeted backfill, never by a heal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.chunker import Chunk, _assign_ordinals
from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_shell_islands import (
    CONF_INTERPOLATED,
    PLACEHOLDER,
    Island,
    island_facets,
    island_statements,
)
from server.code.domain.extract.lang_sql import context_prefix
from server.code.domain.extract.lang_sql import statement_symbol as _statement_symbol
from server.code.domain.extract.lang_sql_dialect import detect_dialect
from server.code.domain.extract.lang_sql_model import Facet, Header, Statement
from server.code.domain.extract.lang_sql_objects import prefix_parts, reference_facets, sql_edges
from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

# the call (or tag) around a literal names the client, the client names the dialect
TS_SQL_CLIENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # any callee whose name carries the store: queryClickhouse, commandClickhouse,
    # upsertClickhouse, clickhouseClient().query — the name is the contract
    (re.compile(r"[cC]lickhouse|ClickHouse"), "clickhouse"),
    (
        re.compile(r"\$queryRaw|\$executeRaw|Prisma\.sql|\bsql$|\bpg\b|\bpool\.query|\bpgClient"),
        "postgres",
    ),
    (re.compile(r"\bmysql\b|\bmysql2\b"), "mysql"),
    (re.compile(r"\bsqlite|better-sqlite|\bdb\.prepare"), "sqlite"),
    (re.compile(r"\bduckdb"), "duckdb"),
    (re.compile(r"\bbigquery|\bbq\."), "bigquery"),
    (re.compile(r"\bsnowflake"), "snowflake"),
)
_FILE_DIALECT_CUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"clickhouse", re.I), "clickhouse"),
    (re.compile(r"prisma|\bpg\b|postgres", re.I), "postgres"),
    (re.compile(r"mysql", re.I), "mysql"),
    (re.compile(r"sqlite", re.I), "sqlite"),
)
_SQL_HEAD_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*"
    r"(SELECT|INSERT|CREATE|DROP|ALTER|WITH|UPDATE|DELETE|OPTIMIZE|SYSTEM|TRUNCATE|EXPLAIN|"
    r"SHOW|DESCRIBE|GRANT|REVOKE|ATTACH|DETACH|COPY|ANALYZE|VACUUM|PRAGMA)\b",
    re.I | re.S,
)
# The keywords that follow a verb in a statement and never in a UI string: `Delete ${n}
# items?` and `Show ${n} more` open with a verb too (105 of langfuse's 489 verb-headed
# literals are prose).
_SQL_KEYWORD_RE = re.compile(
    r"\b(FROM|INTO|WHERE|SET|TABLE|JOIN|VALUES|LIMIT|ORDER\s+BY|GROUP\s+BY|FINAL|PREWHERE|"
    r"SETTINGS|FORMAT|VIEW|DATABASE|INDEX|PARTITION|HAVING|UNION|EXISTS|RETURNING)\b",
    re.I,
)
_ANCESTOR_DEPTH = 8
# A client's name sits at the start or the end of a callee's text; a chain of tagged
# templates makes each enclosing callee as long as the rest of the file
# not a setting: changing it changes the stored island dialects
_CALLEE_END_BYTES = 64
VIA_TEMPLATE = "template"


@dataclass
class TsUnits:
    statements: list[Statement]
    scopes: list[str]  # the qualified name each statement's symbol is scoped under
    symbols: list[Symbol]
    chunks: list[Chunk]
    prefixes: dict[int, str]
    facets: list[Facet] = field(default_factory=list)
    islands: list[Island] = field(default_factory=list)


def template_text(node: Any, source: bytes) -> tuple[str, bool]:
    """The literal's text with each `${…}` replaced by the placeholder plus
    the newlines it spanned (line anchors survive), and whether any was."""
    parts: list[str] = []
    interpolated = False
    pos = node.start_byte + 1  # after the opening backtick
    end = node.end_byte - 1
    for child in node.named_children:
        if child.type != "template_substitution":
            continue
        parts.append(source[pos : child.start_byte].decode("utf-8", "replace"))
        inner = node_text(child, source)
        parts.append(PLACEHOLDER + "\n" * inner.count("\n"))
        interpolated = True
        pos = child.end_byte
    if pos < end:
        parts.append(source[pos:end].decode("utf-8", "replace"))
    return "".join(parts), interpolated


def looks_like_sql(text: str, inside_client: bool = False) -> bool:
    """A two-tier rule, measured for TypeScript: inside a SQL client's call any verb (or
    a leading hole) is a query — the client runs what it is handed; a bare literal needs
    the verb AND the grammar around it — an upper-case verb with one SQL keyword after
    it (`SELECT * FROM t`), or any-case verb with two (langfuse writes `select … from
    scores s WHERE` in lower case and passes it by name). `Delete ${n} items?`, `Show
    ${n} more`, `Select all` have the verb and nothing else."""
    head = text.lstrip()
    if inside_client:
        return bool(_SQL_HEAD_RE.match(head)) or head.startswith(PLACEHOLDER)
    m = _SQL_HEAD_RE.match(head)
    if not m:
        return False
    keywords = len(_SQL_KEYWORD_RE.findall(head[m.end() :]))
    return keywords >= 2 or (m.group(1).isupper() and keywords >= 1)


def client_dialect(ancestors: list[Any], source: bytes, seen: dict[tuple[int, int], str]) -> str:
    """The dialect of the client whose call encloses the literal, from the
    callee's text (`queryClickhouse`, `prisma.$queryRaw`, a `sql` tag). `ancestors`
    runs from the root to the literal's parent, kept by the walk: a node's `parent`
    is found from the root down, so reading it per literal is quadratic in a deep tree.
    `seen` keeps each callee's dialect for the file's next literals."""
    for cur in reversed(ancestors[-_ANCESTOR_DEPTH:]):
        if cur.type == "call_expression":
            fn = cur.child_by_field_name("function")
            if fn is None:
                continue
            key = (fn.start_byte, fn.end_byte)
            if key not in seen:
                callee = _callee_ends(fn, source)
                seen[key] = next((d for pat, d in TS_SQL_CLIENTS if pat.search(callee)), "")
            if seen[key]:
                return seen[key]
    return ""


def _callee_ends(fn: Any, source: bytes) -> str:
    """The callee's text, or its first and last `_CALLEE_END_BYTES` when longer."""
    start, end = fn.start_byte, fn.end_byte
    if end - start <= 2 * _CALLEE_END_BYTES:
        return node_text(fn, source)
    ends = source[start : start + _CALLEE_END_BYTES] + b" " + source[end - _CALLEE_END_BYTES : end]
    return ends.decode("utf-8", "replace")


def file_dialect(pf: ParsedFile) -> str:
    """What the file's imports say it talks to, '' when nothing does."""
    for spec in pf.includes:
        for pat, dialect in _FILE_DIALECT_CUES:
            if pat.search(spec):
                return dialect
    return ""


def find_islands(pf: ParsedFile) -> list[Island]:
    """Every SQL-shaped template literal of the file, in source order."""
    out: list[Island] = []
    if pf.tree is None:
        return out
    source = pf.source
    default = file_dialect(pf)

    ancestors: list[Any] = []
    callees: dict[tuple[int, int], str] = {}

    def walk(node: Any) -> None:
        ancestors.append(node)
        for child in node.named_children:
            if child.type == "template_string":
                text, interpolated = template_text(child, source)
                client = client_dialect(ancestors, source, callees)
                if looks_like_sql(text, inside_client=bool(client)):
                    dialect = client or detect_dialect(text)[0] or default
                    out.append(
                        Island(
                            text=text,
                            line=child.start_point[0] + 1,
                            via=VIA_TEMPLATE,
                            interpolated=interpolated,
                            dialect=dialect or "sql",
                        )
                    )
                continue
            walk(child)
        ancestors.pop()

    walk(pf.tree.root_node)
    return out


def _enclosing_function(pf: ParsedFile, line: int) -> Symbol | None:
    best: Symbol | None = None
    for s in pf.symbols:
        if s.kind == "function" and s.start_line <= line <= s.end_line:
            if best is None or (s.end_line - s.start_line) < (best.end_line - best.start_line):
                best = s
    return best


def island_records(pf: ParsedFile) -> tuple[list[Statement], list[str], list[Island]]:
    """(statements, scope per statement, islands): a statement is scoped
    under the function that holds its literal, else under the module."""
    islands = find_islands(pf)
    pairs = island_statements(islands)
    mod = module_scope(pf.path)
    scopes: list[str] = []
    for st, _isl in pairs:
        fn = _enclosing_function(pf, st.start_line)
        scopes.append(fn.qualified_name if fn is not None else mod)
    return [st for st, _ in pairs], scopes, islands


def ts_units(pf: ParsedFile, chunks: list[Chunk]) -> TsUnits:
    """Island statement symbols, facets and per-chunk context prefixes for
    one parsed TypeScript file; `chunks` is the AST chunker's output."""
    statements, scopes, islands = island_records(pf)
    symbols = [
        _statement_symbol(pf.path, sc, st) for st, sc in zip(statements, scopes, strict=True)
    ]
    by_ordinal = {st.ordinal: sym.id for st, sym in zip(statements, symbols, strict=True)}
    prefixes: dict[int, str] = {}
    for i, c in enumerate(chunks):
        inside = [st for st in statements if c.start_line <= st.start_line <= c.end_line]
        if not inside:
            continue
        ids = [*c.symbol_ids, *(by_ordinal[s.ordinal] for s in inside)]
        c.symbol_ids = list(dict.fromkeys(ids))
        prefix = context_prefix(pf.path, Header(), inside, extras=prefix_parts(inside))
        c.extra_lexemes = " ".join(code_tokens(prefix))
        prefixes[i] = prefix
    _assign_ordinals(chunks)
    pairs = list(zip(statements, islands_by_statement(statements, islands), strict=True))
    facets = [*island_facets(Header(), pairs, islands), *_table_facets(pairs)]
    return TsUnits(
        statements=statements,
        scopes=scopes,
        symbols=symbols,
        chunks=chunks,
        prefixes=prefixes,
        facets=facets,
        islands=islands,
    )


def _table_facets(pairs: list[tuple[Statement, Island]]) -> list[Facet]:
    """The `table_read` / `table_write` / `creates` facets per island statement, floored
    like the statement facets when the statement carried a placeholder, placeholder
    names dropped."""
    out: list[Facet] = []
    for st, isl in pairs:
        for f in reference_facets([st]):
            if PLACEHOLDER in f.name:
                continue
            if isl.interpolated and f.confidence > CONF_INTERPOLATED:
                f = Facet(f.kind, f.name, f.ordinal, f.line, CONF_INTERPOLATED)
            out.append(f)
    return out


def islands_by_statement(statements: list[Statement], islands: list[Island]) -> list[Island]:
    """The island each statement came from, by line containment (an island
    ends where the next begins)."""
    out: list[Island] = []
    for st in statements:
        holder = None
        for isl in islands:
            if isl.line <= st.start_line:
                holder = isl
        out.append(holder or islands[0])
    return out


def narrow_by_dialect(
    name_table: dict[str, list[tuple[str, str, str]]],
    dialect: str,
    scope_dialects: dict[str, str] | None,
) -> dict[str, list[tuple[str, str, str]]]:
    """A name with several table definitions keeps the ones whose defining file speaks
    the island's dialect (the file-level `dialect` facet, keyed by module scope).
    langfuse's Postgres half has `observations`, `traces`, `scores` beside the
    ClickHouse tables of the same names; a ClickHouse query must bind to the ClickHouse
    `CREATE TABLE`, and a name that stays ambiguous after the narrowing stays unbound,
    as the resolver's ladder leaves it."""
    if not dialect or not scope_dialects:
        return name_table
    out: dict[str, list[tuple[str, str, str]]] = {}
    for name, cands in name_table.items():
        if len(cands) > 1:
            kept = [c for c in cands if scope_dialects.get(c[1].split("::", 1)[0]) == dialect]
            cands = kept or cands
        out[name] = cands
    return out


def island_edges(
    pf: ParsedFile,
    statements: list[Statement],
    scopes: list[str],
    name_table: dict[str, list[tuple[str, str, str]]],
    *,
    rw_kinds: bool = False,
    scope_dialects: dict[str, str] | None = None,
) -> list[Edge]:
    """Statement → table / function edges (`sql_edges` over the SQL-gated table,
    narrowed to the island's dialect when a table name is defined in several stores)
    plus the same edges from the enclosing function."""
    by_qname = {s.qualified_name: s for s in pf.symbols}
    dialect_of = {
        st.ordinal: isl.dialect
        for st, isl in zip(
            statements, islands_by_statement(statements, find_islands(pf)), strict=True
        )
    }
    edges: list[Edge] = []
    seen: set[tuple[str, str, str, int]] = set()
    for st, scope in zip(statements, scopes, strict=True):
        fn = by_qname.get(scope)
        table = narrow_by_dialect(name_table, dialect_of.get(st.ordinal, ""), scope_dialects)
        for e in sql_edges(pf.path, scope, [st], [], table, rw_kinds=rw_kinds):
            edges.append(e)
            if fn is None or fn.kind != "function":
                continue
            key = (e.edge_kind, fn.id, e.to_symbol_id, e.line)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                Edge(
                    e.edge_kind,
                    fn.id,
                    e.to_symbol_id,
                    fn.qualified_name,
                    e.to_qualified_name,
                    pf.path,
                    e.line,
                    e.confidence,
                )
            )
    return edges


__all__ = [
    "TS_SQL_CLIENTS",
    "VIA_TEMPLATE",
    "TsUnits",
    "client_dialect",
    "file_dialect",
    "find_islands",
    "island_edges",
    "island_records",
    "looks_like_sql",
    "narrow_by_dialect",
    "template_text",
    "ts_units",
]
