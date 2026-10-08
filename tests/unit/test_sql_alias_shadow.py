"""Static lint: a SELECT alias must not shadow a column the same query filters on.

ClickHouse resolves identifiers in WHERE / PREWHERE / HAVING against SELECT
aliases before table columns (`prefer_column_name_to_alias = 0`, the
default). So

    SELECT toString(memory_id) AS memory_id ... WHERE memory_id IN {ids:Array(UUID)}

compares the String alias to UUIDs and matches nothing, silently, with
green unit tests — the stub client cannot see what ClickHouse resolves.

This test walks every string constant under server/ that looks like SQL
and fails when an expression alias `AS name` (an alias whose left side is
not the bare column `name`) co-occurs with `name` used as a filter operand
in the same statement. Aliases that merely rename an aggregate over the
same column (`sum(x) AS x`) are exempt: the aggregate's inner identifier
resolves to the column and the alias is never a filter operand.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"

# `<expr> AS name` where <expr> is a function call or a query parameter,
# i.e. anything whose type or value can differ from the column `name`.
_EXPR_ALIAS = re.compile(
    r"(?:\)|\{[A-Za-z_][A-Za-z0-9_]*:[A-Za-z0-9_() ,]+\})\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_SQL_HINT = re.compile(r"\bSELECT\b.*\bFROM\b", re.IGNORECASE | re.DOTALL)


def _split_subqueries(stmt: str) -> list[str]:
    """Return the statement with each nested `( SELECT ... )` replaced by a
    placeholder, followed by every nested block (recursively flattened). An
    outer WHERE that filters a subquery's alias is a column reference, not a
    shadow, so each level is linted on its own."""
    out: list[str] = []
    flat = []
    i = 0
    n = len(stmt)
    while i < n:
        if stmt[i] == "(" and re.match(r"\(\s*SELECT\b", stmt[i:], re.IGNORECASE):
            depth = 0
            j = i
            while j < n:
                if stmt[j] == "(":
                    depth += 1
                elif stmt[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = stmt[i + 1 : j]
            out.extend(_split_subqueries(inner))
            flat.append(" __SUBQ__ ")
            i = j + 1
            continue
        flat.append(stmt[i])
        i += 1
    return ["".join(flat), *out]


def _where_clause(sql: str) -> str:
    """The WHERE / PREWHERE text of one flat statement (HAVING is excluded:
    filtering an aggregate alias there is the intended use of aliases)."""
    m = re.search(r"\b(WHERE|PREWHERE)\b", sql, re.IGNORECASE)
    if not m:
        return ""
    tail = sql[m.end() :]
    stop = re.search(
        r"\b(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|SETTINGS|UNION|FORMAT)\b", tail, re.IGNORECASE
    )
    return tail[: stop.start()] if stop else tail


def _filter_operand(where: str, name: str) -> bool:
    """`name IN {`, `name = {`, `name IN (`, `name = 'lit'` inside the WHERE text."""
    return (
        re.search(
            rf"(?<![A-Za-z0-9_.]){re.escape(name)}\s*"
            rf"(?:=|!=|<>|<=|>=|<|>|\bIN\b|\bNOT\s+IN\b)\s*[\({{'\d]",
            where,
            re.IGNORECASE,
        )
        is not None
    )


def _statements(sql: str) -> list[str]:
    """Split on semicolons; each statement is linted alone."""
    return [s for s in sql.split(";") if s.strip()]


def shadowed_aliases(sql: str) -> list[str]:
    """Aliases in `sql` that shadow a column the same SELECT level filters on."""
    hits: list[str] = []
    for stmt in _statements(sql):
        if not _SQL_HINT.search(stmt):
            continue
        for level in _split_subqueries(stmt):
            if not re.search(r"\bSELECT\b", level, re.IGNORECASE):
                continue
            where = _where_clause(level)
            if not where:
                continue
            for m in _EXPR_ALIAS.finditer(level):
                alias = m.group(1)
                if _filter_operand(where, alias):
                    hits.append(alias)
    return hits


def _sql_constants(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SQL_HINT.search(node.value):
                out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            # f-strings: lint the literal parts joined, placeholders elided.
            text = "".join(
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if _SQL_HINT.search(text):
                out.append((node.lineno, text))
    return out


def test_no_select_alias_shadows_a_filtered_column() -> None:
    offenders: list[str] = []
    for path in sorted(SERVER.rglob("*.py")):
        for lineno, sql in _sql_constants(path):
            for alias in shadowed_aliases(sql):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: alias `{alias}`")
    assert not offenders, (
        "A SELECT alias shadows a column the same statement filters on; ClickHouse "
        "resolves WHERE/HAVING against the alias and the filter silently matches "
        "nothing. Rename the alias (`AS mid`).\n" + "\n".join(offenders)
    )


def test_detector_catches_the_fold_back_shape() -> None:
    sql = """
        SELECT toString(memory_id) AS memory_id, org
        FROM loom.memory FINAL
        WHERE org IN {orgs:Array(String)} AND memory_id IN {ids:Array(UUID)}
    """
    assert shadowed_aliases(sql) == ["memory_id"]


def test_detector_catches_the_parameter_alias_shape() -> None:
    sql = "SELECT {org:String} AS org, path FROM loom.code_edges WHERE org = {org:String}"
    assert shadowed_aliases(sql) == ["org"]


def test_detector_accepts_the_renamed_alias_and_aggregates() -> None:
    assert (
        shadowed_aliases(
            "SELECT toString(memory_id) AS mid FROM loom.memory WHERE memory_id IN {ids:Array(UUID)}"
        )
        == []
    )
    assert (
        shadowed_aliases(
            "SELECT memory_id, sum(recall_count) AS recall_count FROM loom.memory_usage_totals "
            "WHERE memory_id IN {ids:Array(UUID)} GROUP BY memory_id HAVING recall_count > 0"
        )
        == []
    )
    # Alias present, but never used as a filter operand: fine.
    assert (
        shadowed_aliases(
            "SELECT toString(memory_id) AS memory_id FROM loom.memory WHERE org = {org:String}"
        )
        == []
    )


# `FROM t FINAL AS x` is a syntax error on ClickHouse 26.x ("failed at position
# … (AS)"): the alias goes before the modifier — `FROM t AS x FINAL`. A
# default-off worker's statements are never run against a server by other
# unit tests, so this lint scans the source.
_FINAL_BEFORE_ALIAS = re.compile(r"\bFINAL\s+AS\s+[A-Za-z_]", re.IGNORECASE)


def test_final_comes_after_the_table_alias() -> None:
    offenders: list[str] = []
    for path in sorted(SERVER.rglob("*.py")):
        for lineno, sql in _sql_constants(path):
            if _FINAL_BEFORE_ALIAS.search(sql):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "`FROM t FINAL AS x` does not parse on ClickHouse 26.x; write `FROM t AS x FINAL`.\n"
        + "\n".join(offenders)
    )


def test_final_detector_shapes() -> None:
    assert _FINAL_BEFORE_ALIAS.search("SELECT 1 FROM loom.memory FINAL AS m WHERE 1")
    assert not _FINAL_BEFORE_ALIAS.search("SELECT 1 FROM loom.memory AS m FINAL WHERE 1")
    assert not _FINAL_BEFORE_ALIAS.search("SELECT 1 FROM loom.memory FINAL WHERE 1")
