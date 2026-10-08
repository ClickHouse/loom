"""A tracker's own schema, mapped onto loom's logical model.

Loom's tracker statements are written against one *logical* model — tables
`github_issues`, `github_prs`, `edges` and the optional ones in
`contract.OPTIONAL`, with the columns `columns.COLUMNS_READ` lists. That
model is loom's vocabulary, not any team's schema. A tracker either exposes
it directly (a database of views with those names — the identity case, no
mapping at all) or carries a **mapping**: for each logical table, which of the
team's tables it reads and, for each logical column, an expression over that
table's columns. Loom renders the mapping as a projection
`(SELECT <expr> AS <column>, … FROM db.table [FINAL])` and substitutes it for
the logical table in every statement (`ContractReport.rewrite`), so the ~90
statements run unchanged against any schema.

Safety: a mapping is data, never SQL. An expression is a column name the live
schema has, a literal, or one of a few forms (`concat`, `coalesce`, `eq`,
`in`, `has`, `cast`, `lower`, `digits`), each rendered by this module with identifiers
backquoted and literals quoted. Reads still run with the tracker's read-only
credential.

What loom decides, not the mapping: an item's `id` is always
`uuid5(NAMESPACE_DNS, "<kind>:<source_id>")` — loom derives it locally to walk
`edges` — so it is rendered from the mapped `source_id`, and an edge's
`from_id` / `to_id` from its ends' mapped references.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from server.tracker.domain.contract import concrete_type, default_expr, family_ok, uuid5_sql

MAPPING_VERSION = 1

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_TABLE_REF = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}(\.[A-Za-z_][A-Za-z0-9_]{0,127})?$")
_FAMILIES = ("String", "UInt", "Int", "Float", "DateTime", "UUID", "JSON")
_REPLACING = ("Replacing", "Collapsing", "VersionedCollapsing", "Aggregating", "Summing")


class MappingError(ValueError):
    """A mapping that cannot be rendered safely or does not fit the live
    schema; the message names the table and column."""


# --- expressions --------------------------------------------------------------


def quote_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise MappingError(f"{name!r} is not a column name")
    return f"`{name}`"


def quote_literal(value: Any) -> str:
    if isinstance(value, dict) and set(value) == {"lit"}:
        value = value["lit"]
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    raise MappingError(f"{value!r} is not a literal (string, number or boolean)")


def family_of_type(ch_type: str) -> str:
    """A live ClickHouse type as a contract family ('' when unknown)."""
    t = ch_type.strip()
    for wrap in ("LowCardinality(", "Nullable("):
        while t.startswith(wrap) and t.endswith(")"):
            t = t[len(wrap) : -1]
    if t.startswith("Array(") and t.endswith(")"):
        inner = family_of_type(t[6:-1])
        return f"Array({inner})" if inner else ""
    for fam in ("UUID", "DateTime", "Float", "UInt", "Int", "JSON", "String"):
        if family_ok(fam, t):
            return fam
    return ""


def expr_family(expr: Any, columns: dict[str, str]) -> str:
    """The family an expression yields over a table's live `columns`
    (name → ClickHouse type). Raises MappingError on an unknown column."""
    if isinstance(expr, str):
        if expr not in columns:
            raise MappingError(f"no column {expr!r}")
        return family_of_type(columns[expr]) or "String"
    if not isinstance(expr, dict) or len(expr) != 1:
        raise MappingError(f"{expr!r} is not an expression")
    ((op, arg),) = expr.items()
    if op == "col":
        return expr_family(str(arg), columns)
    if op == "lit":
        if isinstance(arg, bool) or isinstance(arg, int):
            return "UInt" if (isinstance(arg, bool) or arg >= 0) else "Int"
        return "Float" if isinstance(arg, float) else "String"
    if op in ("concat", "lower"):
        return "String"
    if op in ("eq", "in", "has", "digits"):
        return "UInt"
    if op == "coalesce":
        if not isinstance(arg, list) or not arg:
            raise MappingError("coalesce takes a list")
        return expr_family(arg[0], columns)
    if op == "cast":
        if not isinstance(arg, list) or len(arg) != 2 or str(arg[1]) not in _FAMILIES:
            raise MappingError(f"cast takes [expression, one of {_FAMILIES}]")
        return str(arg[1])
    raise MappingError(f"unknown expression form {op!r}")


def render(expr: Any, columns: dict[str, str]) -> str:
    """The SQL of a mapping expression over a table's live columns."""
    expr_family(expr, columns)  # validates every column and form first
    if isinstance(expr, str):
        return quote_ident(expr)
    ((op, arg),) = expr.items()
    if op == "col":
        return quote_ident(str(arg))
    if op == "lit":
        return quote_literal(arg)
    if op == "concat":
        if not isinstance(arg, list) or not arg:
            raise MappingError("concat takes a list")
        return "concat(" + ", ".join(f"toString({render(a, columns)})" for a in arg) + ")"
    if op == "lower":
        return f"lower(toString({render(arg, columns)}))"
    if op == "digits":
        # the trailing number of a key: 'HD-7' → 7, 'PROJ-1234' → 1234 (Jira-style keys)
        return f"toUInt64OrZero(extract(toString({render(arg, columns)}), '(\\d+)$'))"
    if op == "coalesce":
        return "coalesce(" + ", ".join(render(a, columns) for a in arg) + ")"
    if op == "eq":
        left, value = _pair(arg, "eq")
        return f"toUInt8({render(left, columns)} = {quote_literal(value)})"
    if op == "in":
        left, values = _pair(arg, "in")
        if not isinstance(values, list) or not values:
            raise MappingError("in takes [expression, [values…]]")
        listed = ", ".join(quote_literal(v) for v in values)
        return f"toUInt8({render(left, columns)} IN ({listed}))"
    if op == "has":
        left, value = _pair(arg, "has")
        return f"toUInt8(has({render(left, columns)}, {quote_literal(value)}))"
    if op == "cast":
        return f"CAST({render(arg[0], columns)} AS {concrete_type(str(arg[1]))})"
    raise MappingError(f"unknown expression form {op!r}")


def _pair(arg: Any, op: str) -> tuple[Any, Any]:
    if not isinstance(arg, list) or len(arg) != 2:
        raise MappingError(f"{op} takes [expression, value]")
    return arg[0], arg[1]


def _fit(sql: str, have: str, want: str) -> str:
    """`sql` converted to the family a statement reads (a String column
    where the contract reads UInt, a Date where it reads DateTime64 …)."""
    if want.startswith("Array("):
        return sql if have == want else f"CAST({sql} AS {concrete_type(want)})"
    if have and family_ok(
        want,
        {"UInt": "UInt64", "Int": "Int64", "Float": "Float64", "DateTime": "DateTime64(3)"}.get(
            have, have
        ),
    ):
        if want == "DateTime":
            return f"toDateTime64({sql}, 3)"
        return sql
    if want == "String":
        return f"toString({sql})"
    if want == "DateTime":
        return f"parseDateTime64BestEffortOrZero(toString({sql}), 3)"
    if want in ("UInt", "Int", "Float"):
        return f"to{concrete_type(want)}OrZero(toString({sql}))"
    return f"CAST({sql} AS {concrete_type(want)})"


# --- the mapping ----------------------------------------------------------------


@dataclass(frozen=True)
class TableMap:
    """One logical table read from one of the team's tables."""

    source: str  # `db.table` or `table` (the tracker's database)
    columns: dict[str, Any] = field(default_factory=dict)  # logical column → expression
    final: bool = False  # read with FINAL (a Replacing/Collapsing engine)

    def ref(self, database: str) -> str:
        if not _TABLE_REF.match(self.source):
            raise MappingError(f"{self.source!r} is not a table name")
        db, _, table = self.source.rpartition(".")
        return f"{quote_ident(db or database)}.{quote_ident(table)}"


@dataclass(frozen=True)
class SourceMapping:
    tables: dict[str, TableMap] = field(default_factory=dict)
    version: int = MAPPING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tables": {
                t: {"source": m.source, "final": m.final, "columns": dict(m.columns)}
                for t, m in sorted(self.tables.items())
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def parse(raw: Any) -> SourceMapping | None:
    """A stored or submitted mapping (dict or JSON text); None for none.
    Raises MappingError on a shape it cannot read — never a partial mapping."""
    if raw in (None, "", {}):
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as e:
        raise MappingError(f"a mapping is JSON: {e}") from e
    if not isinstance(data, dict):
        raise MappingError("a mapping is an object")
    try:
        version = int(data.get("version") or MAPPING_VERSION)
    except (TypeError, ValueError) as e:
        raise MappingError(f"mapping version {data.get('version')!r} is not a number") from e
    if version != MAPPING_VERSION:
        raise MappingError(f"mapping version {version} is not {MAPPING_VERSION}")
    tables_raw = data.get("tables") or {}
    if not isinstance(tables_raw, dict) or not tables_raw:
        raise MappingError("a mapping names at least one table")
    from server.tracker.domain.columns import LOGICAL_TABLES

    tables: dict[str, TableMap] = {}
    for logical, spec in tables_raw.items():
        if logical not in LOGICAL_TABLES:
            raise MappingError(f"{logical!r} is not a table loom reads")
        if not isinstance(spec, dict) or not str(spec.get("source") or ""):
            raise MappingError(f"{logical}: `source` names the table it reads")
        cols = spec.get("columns") or {}
        if not isinstance(cols, dict):
            raise MappingError(f"{logical}: `columns` is an object")
        tables[logical] = TableMap(
            source=str(spec["source"]), columns=dict(cols), final=bool(spec.get("final"))
        )
    return SourceMapping(tables=tables, version=version)


def final_for_engine(engine: str) -> bool:
    """Whether reads of a table with this engine should dedupe with FINAL."""
    e = engine or ""
    return any(k in e for k in _REPLACING)


# --- rendering a mapped table ---------------------------------------------------

Live = dict[str, dict[str, str]]  # `db.table` → column → ClickHouse type


def _live_columns(tmap: TableMap, database: str, live: Live) -> dict[str, str]:
    key = tmap.source if "." in tmap.source else f"{database}.{tmap.source}"
    cols = live.get(key)
    if cols is None:
        raise MappingError(f"{tmap.source!r} is not a table this credential can read")
    return cols


def _mapped(tmap: TableMap, col: str) -> bool:
    return tmap.columns.get(col) not in (None, "")


def derived_default(logical: str, col: str, tmap: TableMap, cols: dict[str, str]) -> str | None:
    """An unmapped column loom can still say from the mapped
    ones — generic facts about issues and PRs, not any one tracker's: a PR's
    state from whether it merged and closed, when it merged from when it
    closed, when an item last changed from when it opened and closed, a PR's
    number from its PR number. None = the typed default."""

    def m(c: str, fam: str) -> str:
        return _column_sql(logical, c, fam, tmap, cols)

    epoch = "toDateTime64(0, 3)"
    if logical == "github_prs":
        if col == "state" and (_mapped(tmap, "is_merged") or _mapped(tmap, "closed_at")):
            merged = m("is_merged", "UInt") if _mapped(tmap, "is_merged") else "toUInt64(0)"
            return (
                f"multiIf({merged} = 1, 'merged', {m('closed_at', 'DateTime')} > {epoch}, "
                "'closed', 'open')"
            )
        if col == "merged_at" and _mapped(tmap, "is_merged") and _mapped(tmap, "closed_at"):
            return f"if({m('is_merged', 'UInt')} = 1, {m('closed_at', 'DateTime')}, {epoch})"
        if col == "is_merged" and _mapped(tmap, "state"):
            return f"toUInt64({m('state', 'String')} = 'merged')"
        if col == "number" and _mapped(tmap, "pr_number"):
            return m("pr_number", "UInt")
        if col == "pr_number" and _mapped(tmap, "number"):
            return m("number", "UInt")
    if (
        logical in ("github_issues", "github_prs")
        and col == "updated_at"
        and _mapped(tmap, "created_at")
    ):
        created = m("created_at", "DateTime")
        closed = m("closed_at", "DateTime") if _mapped(tmap, "closed_at") else epoch
        return f"greatest({created}, {closed})"
    return None


def _column_sql(logical: str, col: str, want: str, tmap: TableMap, cols: dict[str, str]) -> str:
    expr = tmap.columns.get(col)
    if expr is None or expr == "":
        return derived_default(logical, col, tmap, cols) or default_expr(want)
    try:
        fitted = _fit(render(expr, cols), expr_family(expr, cols), want)
        # a Nullable column reads its default where it is NULL: the statements
        # compare `closed_at` with epoch 0, `labels` with [], never with NULL
        return f"ifNull({fitted}, {default_expr(want)})"
    except MappingError as e:
        raise MappingError(f"{logical}.{col}: {e}") from None


def table_sql(logical: str, tmap: TableMap, database: str, live: Live, *, final: bool) -> str:
    """`(SELECT <every column the statements read> FROM <the team's table>)`
    — mapped columns as their expressions, fitted to the family the
    statements read; the rest typed defaults; ids derived."""
    from server.tracker.domain.columns import COLUMNS_READ, DERIVED_IDS

    cols = _live_columns(tmap, database, live)
    parts: list[str] = []
    for col, want in COLUMNS_READ[logical].items():
        if col == "id" and logical in DERIVED_IDS:
            kind, twin = DERIVED_IDS[logical]
            ref = _column_sql(logical, "source_id", "String", tmap, cols)
            prefix = (
                f"if({_column_sql(logical, 'is_private', 'UInt', tmap, cols)} = 1, "
                f"'{kind}_private:', '{kind}:')"
                if twin
                else f"'{kind}:'"
            )
            parts.append(f"{uuid5_sql(f'concat({prefix}, {ref})')} AS `id`")
        elif logical == "edges" and col in ("from_id", "to_id"):
            end = col.split("_")[0]
            typ = _column_sql(logical, f"{end}_type", "String", tmap, cols)
            ref = _column_sql(logical, f"{end}_ref", "String", tmap, cols)
            parts.append(f"{uuid5_sql(f'concat({typ}, {chr(39)}:{chr(39)}, {ref})')} AS `{col}`")
        else:
            parts.append(f"{_column_sql(logical, col, want, tmap, cols)} AS `{col}`")
    fin = " FINAL" if final and tmap.final else ""
    return f"(SELECT {', '.join(parts)} FROM {tmap.ref(database)}{fin})"


def stub_sql(logical: str) -> str:
    """An empty table with the logical columns: a feature over a table the
    tracker does not map answers nothing instead of failing."""
    from server.tracker.domain.columns import COLUMNS_READ

    parts = ", ".join(f"{default_expr(f)} AS `{c}`" for c, f in COLUMNS_READ[logical].items())
    return f"(SELECT {parts} FROM system.one WHERE 0)"


@dataclass
class MappingCheck:
    """What a mapping gives, checked against the live schema: the virtual
    schema the contract is diffed against, what defaults, what is missing."""

    described: dict[str, dict[str, str]] = field(default_factory=dict)
    defaulted: dict[str, list[str]] = field(default_factory=dict)
    core_missing: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def check(mapping: SourceMapping, database: str, live: Live) -> MappingCheck:
    """The mapping over the live schema, pure: every expression validated,
    each logical table's columns typed (mapped ones by their expression,
    unmapped ones by the family they default to)."""
    from server.tracker.domain.columns import COLUMNS_READ, CORE, DERIVED_IDS

    out = MappingCheck()
    for logical, tmap in sorted(mapping.tables.items()):
        try:
            cols = _live_columns(tmap, database, live)
        except MappingError as e:
            out.errors.append(f"{logical}: {e}")
            continue
        typed: dict[str, str] = {}
        defaulted: list[str] = []
        for col, want in COLUMNS_READ[logical].items():
            expr = tmap.columns.get(col)
            if (col == "id" and logical in DERIVED_IDS) or (
                logical == "edges" and col in ("from_id", "to_id")
            ):
                typed[col] = want  # loom derives it
                continue
            if expr in (None, ""):
                if derived_default(logical, col, tmap, cols) is None:
                    defaulted.append(col)
                typed[col] = want
                continue
            try:
                expr_family(expr, cols)
                typed[col] = want  # fitted to the family the statements read
            except MappingError as e:
                out.errors.append(f"{logical}.{col}: {e}")
        for col, expr in tmap.columns.items():
            if col not in COLUMNS_READ[logical] and not (
                logical == "edges" and col in ("from_ref", "to_ref")
            ):
                out.errors.append(f"{logical}.{col}: not a column loom reads")
            elif logical == "edges" and col in ("from_ref", "to_ref") and expr not in (None, ""):
                try:
                    expr_family(expr, cols)
                except MappingError as e:
                    out.errors.append(f"{logical}.{col}: {e}")
        missing = [c for c in CORE.get(logical, ()) if tmap.columns.get(c) in (None, "")]
        if missing:
            out.core_missing[logical] = missing
        out.described[logical] = typed
        if defaulted:
            out.defaulted[logical] = defaulted
    if "github_issues" not in mapping.tables:
        out.core_missing.setdefault("github_issues", list(CORE["github_issues"]))
    return out
