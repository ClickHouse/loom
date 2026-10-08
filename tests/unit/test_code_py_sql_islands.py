"""The SQL a Python file runs as statement records, facets and edges."""

from __future__ import annotations

import pytest

from server.code.domain.extract import lang_py_islands as pi
from server.code.domain.extract.lang_python import parse_python
from server.code.domain.extract.lang_sql_objects import sql_edges
from server.code.domain.extract.name_scope import gate_name_table

PATH = "tests/integration/test_islands/test.py"


def _units(src: str, path: str = PATH) -> pi.PySqlUnits:
    return pi.py_sql_units(parse_python(path, src.encode()))


def test_client_call_arguments_become_statements_anchored_at_their_line() -> None:
    src = (
        "def test_x(node):\n"
        '    node.query("CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY x")\n'
        '    node.query_and_get_error("DROP TABLE missing")\n'
        '    r = node.query("""\n'
        "        SELECT count() FROM t\n"
        "        SETTINGS max_threads = 1\n"
        '    """)\n'
        "    assert r == '0\\n'\n"
    )
    u = _units(src)
    assert [(i.via, i.line, i.dialect) for i in u.islands] == [
        ("py_call", 2, "clickhouse"),
        ("py_call", 3, "clickhouse"),
        ("py_call", 4, "clickhouse"),
    ]
    kinds = [(s.kind, s.qualified_name.rsplit("::", 1)[1]) for s in u.symbols]
    assert ("statement", "stmt_0001") in kinds and ("table", "t") in kinds
    stmt3 = next(s for s in u.symbols if s.qualified_name.endswith("stmt_0003"))
    assert (stmt3.start_line, stmt3.end_line) == (5, 6)  # re-anchored to the file's lines
    names = {(f.kind, f.name) for f in u.facets}
    assert {
        ("statement_kind", "CREATE TABLE"),
        ("engine", "MergeTree"),
        ("creates", "t"),
        ("table_read", "t"),
        ("setting", "max_threads"),
        ("dialect", "clickhouse"),
    } <= names


def test_holes_become_the_placeholder_and_lower_confidence() -> None:
    src = (
        "def test_x(node, db):\n"
        '    node.query(f"INSERT INTO {db}.t VALUES ({1})")\n'
        '    node.query("SELECT * FROM {} FINAL".format(db))\n'
        '    node.query("OPTIMIZE TABLE " + db + ".t")\n'
        '    cur.execute("UPDATE users SET n = %s WHERE id = %(id)s", (1, {"id": 2}))\n'
        '    cur.execute("DELETE FROM users WHERE id = ?", (3,))\n'
    )
    u = _units(src)
    texts = [i.text for i in u.islands]
    assert texts[0] == f"INSERT INTO {pi.PLACEHOLDER}.t VALUES ({pi.PLACEHOLDER})"
    assert texts[1] == f"SELECT * FROM {pi.PLACEHOLDER} FINAL"
    assert texts[2] == f"OPTIMIZE TABLE {pi.PLACEHOLDER}.t"
    assert texts[3] == f"UPDATE users SET n = {pi.PLACEHOLDER} WHERE id = {pi.PLACEHOLDER}"
    assert texts[4] == f"DELETE FROM users WHERE id = {pi.PLACEHOLDER}"
    assert all(i.interpolated for i in u.islands)
    # a hole never names a table or an object
    assert not any(pi.PLACEHOLDER in f.name for f in u.facets)
    assert not any(pi.PLACEHOLDER in s.qualified_name for s in u.symbols)
    assert {("table_write", "users")} <= {(f.kind, f.name) for f in u.facets}
    kinds = [f for f in u.facets if f.kind == "statement_kind"]
    assert kinds and all(f.confidence == pi.CONF_INTERPOLATED for f in kinds)


def test_bare_sql_literals_count_but_docstrings_and_prose_do_not() -> None:
    src = (
        '"""Create a table from Arrow data."""\n'
        "\n"
        'QUERY = "SELECT id, name FROM users WHERE id = 1"\n'
        "\n"
        "def f(node):\n"
        '    """Select the rows and drop the table."""\n'
        '    msg = "Select the broker before creating the table"\n'
        '    log = "CREATE"\n'
        "    return node.query(QUERY) + msg + log\n"
    )
    u = _units(src)
    assert [(i.via, i.line) for i in u.islands] == [("py_literal", 3)]
    assert u.islands[0].dialect == ""  # no client named one, no text cue: undecided
    assert not any(f.kind == "dialect" for f in u.facets)


def test_keyword_arguments_and_the_text_client_are_read() -> None:
    src = (
        "def f(session, conn):\n"
        '    session.execute(text("SELECT id FROM orders WHERE created_at > :since"), {"since": 1})\n'
        '    conn.execute(sql="INSERT INTO orders (id) VALUES (1)")\n'
        '    conn.execute(1, query="TRUNCATE TABLE orders")\n'
    )
    u = _units(src)
    assert [i.text for i in u.islands] == [
        "SELECT id FROM orders WHERE created_at > :since",
        "INSERT INTO orders (id) VALUES (1)",
        "TRUNCATE TABLE orders",
    ]
    assert {("table_read", "orders"), ("table_write", "orders")} <= {
        (f.kind, f.name) for f in u.facets
    }


def test_generic_clients_take_the_texts_dialect() -> None:
    src = (
        "def f(cur):\n"
        '    cur.execute("CREATE TABLE t (id SERIAL PRIMARY KEY, d JSONB)")\n'
        '    cur.execute("SELECT 1::int")\n'
    )
    u = _units(src)
    assert {i.dialect for i in u.islands} == {"postgres"}
    assert ("dialect", "postgres") in {(f.kind, f.name) for f in u.facets}


def test_island_statements_bind_to_the_tables_the_file_creates() -> None:
    src = (
        "def test_x(node):\n"
        '    node.query("CREATE TABLE src (x UInt8) ENGINE = Memory")\n'
        '    node.query("CREATE MATERIALIZED VIEW mv TO dst AS SELECT x FROM src")\n'
        '    node.query("INSERT INTO src VALUES (1)")\n'
        '    node.query("SELECT * FROM src")\n'
    )
    pf = parse_python(PATH, src.encode())
    u = pi.py_sql_units(pf)
    pf.symbols.extend(u.symbols)
    edges = sql_edges(
        PATH,
        "tests.integration.test_islands.test",
        u.statements,
        pf.symbols,
        gate_name_table({}, "sql"),
        rw_kinds=True,
    )
    by_kind = {}
    for e in edges:
        by_kind.setdefault(e.edge_kind, []).append(e)
    src_id = next(s.id for s in u.symbols if s.kind == "table")
    assert [e.to_symbol_id for e in by_kind["writes"]] == [src_id]  # the INSERT
    assert {e.to_symbol_id for e in by_kind["reads"]} == {src_id}  # the MV's SELECT, the SELECT


def test_ingest_gate_off_leaves_a_python_file_as_its_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_parse
    from server.settings import settings

    src = 'def f(node):\n    node.query("CREATE TABLE t (x UInt8) ENGINE = Memory")\n'
    monkeypatch.setattr(settings, "code_py_sql_islands", False, raising=False)
    pf = ingest_parse.parse_file(PATH, src.encode())
    assert pf is not None and [s.kind for s in pf.symbols] == ["function"]
    assert ingest_parse._facets_ride_symbols(pf, PATH, False) is False
    monkeypatch.setattr(settings, "code_py_sql_islands", True, raising=False)
    pf = ingest_parse.parse_file(PATH, src.encode())
    assert pf is not None and sorted(s.kind for s in pf.symbols) == [
        "function",
        "statement",
        "table",
    ]
    assert ingest_parse._facets_ride_symbols(pf, PATH, False) is True
    plain = ingest_parse.parse_file("app/util.py", b"def g():\n    return 1\n")
    assert (
        plain is not None
        and ingest_parse._facets_ride_symbols(plain, "app/util.py", False) is False
    )
