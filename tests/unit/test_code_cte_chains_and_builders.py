"""CTE built on CTE and re-aliased CTEs carry columns;
StringBuilder.append chains and "".join lists are assembled islands; a Go
package with no conventional file name resolves through a listing."""

from __future__ import annotations

from server.code.domain.extract import lang_sql_columns as lc
from server.code.domain.extract.lang_go import go_package_dir, go_package_file
from server.code.domain.extract.lang_java import java_sql_units, parse_java
from server.code.domain.extract.lang_py_islands import py_sql_units
from server.code.domain.extract.lang_python import parse_python
from server.code.domain.extract.lang_sql_split import split_statements

DDL = "CREATE TABLE users (id UInt64, email String, created DateTime);\n"


def _reads(sql: str) -> list[tuple[str, str, float]]:
    sts = split_statements(DDL + sql)
    return lc.column_refs(sts[1], lc.file_columns(sts)).reads


def test_cte_built_on_cte_and_re_aliased_cte_carry_columns() -> None:
    chained = _reads(
        "WITH a AS (SELECT id, email FROM users), b AS (SELECT id, email AS mail FROM a) "
        "SELECT b.mail FROM b;"
    )
    assert ("users", "email", 0.95) in chained  # b.mail → a.email → users.email
    realiased = _reads(
        "WITH recent AS (SELECT id, email FROM users) SELECT r2.email FROM recent AS r2;"
    )
    assert ("users", "email", 0.95) in realiased
    # a CTE column the body could not source (an expression) carries nothing
    expr = _reads("WITH a AS (SELECT id, lower(email) AS e FROM users) SELECT a.e FROM a;")
    assert not any(c == "e" for _, c, _ in expr)


def test_stringbuilder_chain_across_statements_is_one_island() -> None:
    java = (
        "class R {\n  String q(int id) {\n"
        '    StringBuilder sb = new StringBuilder("SELECT id, name");\n'
        '    sb.append(" FROM users").append(" WHERE id = ").append(id);\n'
        '    sb.append(" ORDER BY id");\n'
        "    return jdbc.queryForObject(sb.toString(), String.class);\n"
        "  }\n}\n"
    )
    u = java_sql_units(parse_java("R.java", java.encode()))
    assert [(i.via, i.line, i.text) for i in u.islands] == [
        ("assembled", 3, "SELECT id, name FROM users WHERE id = __var__ ORDER BY id"),
    ]
    assert ("table_read", "users") in {(f.kind, f.name) for f in u.facets}


def test_python_join_list_is_one_island() -> None:
    src = (
        "def f(node, db):\n"
        '    q = " ".join(["SELECT id, name", "FROM users", f"WHERE db = \'{db}\'"])\n'
        "    node.query(q)\n"
        '    lines = "\\n".join(["Delete this", "now"])\n'
    )
    u = py_sql_units(parse_python("t.py", src.encode()))
    assert [(i.via, i.line, i.interpolated, i.text) for i in u.islands] == [
        ("assembled", 2, True, "SELECT id, name FROM users WHERE db = '__var__'"),
    ]


def test_go_package_file_from_a_directory_listing() -> None:
    assert go_package_dir("example.com/app/internal/store", "example.com/app") == "internal/store"
    assert go_package_dir("example.com/app/internal/store", "example.com/app", "go/app") == (
        "go/app/internal/store"
    )
    assert go_package_dir("fmt", "example.com/app") == ""
    listing = [
        "internal/store/store_test.go",
        "internal/store/sqlite.go",
        "internal/store/pg.go",
        "internal/store/sub/x.go",
        "internal/store/README.md",
    ]
    assert (
        go_package_file(listing, "internal/store") == "internal/store/pg.go"
    )  # first non-test .go
    assert go_package_file(["internal/store/store_test.go"], "internal/store") == ""
