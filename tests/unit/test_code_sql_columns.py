"""Column-level lineage — definitions, bound references, symbols, edges."""

from __future__ import annotations

import time

import pytest

from server.code.domain.extract import lang_sql_columns as lc
from server.code.domain.extract.lang_sql_objects import references
from server.code.domain.extract.lang_sql_split import split_statements
from tests.unit.code_fixtures import assert_linear

DDL = """
CREATE TABLE users (id UInt64, email String DEFAULT '', created DateTime CODEC(ZSTD),
    PRIMARY KEY (id), INDEX ix email TYPE bloom_filter GRANULARITY 1) ENGINE = MergeTree ORDER BY id;
CREATE TABLE orders (`id` UInt64, "user_id" UInt64, total Decimal(10,2), n Nested(a UInt8, b String))
    ENGINE = MergeTree ORDER BY id;
CREATE TABLE t2 AS users;
CREATE TABLE t3 ENGINE = Memory AS SELECT id FROM users;
ALTER TABLE users ADD COLUMN IF NOT EXISTS name String, DROP COLUMN created, RENAME COLUMN email TO mail;
"""


def _known(sql: str):
    sts = split_statements(sql)
    return sts, lc.file_columns(sts)


def test_create_table_bodies_define_columns_and_skip_constraints() -> None:
    sts = split_statements(DDL)
    assert [c.key for c in lc.table_columns(sts[0])] == ["users.id", "users.email", "users.created"]
    assert [c.key for c in lc.table_columns(sts[1])] == [
        "orders.id",
        "orders.user_id",
        "orders.total",
        "orders.n",
    ]
    assert lc.table_columns(sts[2]) == [] and lc.table_columns(sts[3]) == []  # AS t / AS SELECT
    added, written = lc.column_changes(sts[4])
    assert [c.key for c in added] == ["users.name", "users.mail"]
    assert [c.key for c in written] == ["users.created", "users.email"]
    known = lc.file_columns(sts)
    assert known == {
        "users": {"id", "email", "created", "name", "mail"},
        "orders": {"id", "user_id", "total", "n"},
    }


def test_writes_are_explicit_and_reads_bind_by_alias_or_single_table() -> None:
    sts, known = _known(
        DDL
        + """
INSERT INTO users (id, email) VALUES (1, 'a');
UPDATE users SET email = 'x', name = 'y' WHERE id = 1;
ALTER TABLE users UPDATE email = 'z' WHERE id = 2;
SELECT u.email, o.total FROM users u JOIN orders AS o ON o.user_id = u.id WHERE u.created > now();
SELECT email, count() FROM users FINAL WHERE id > 10 GROUP BY email ORDER BY email DESC LIMIT 1;
SELECT id FROM users u, orders AS o WHERE u.id = o.user_id AND o.total > 1;
DELETE FROM orders WHERE total > 100;
"""
    )
    by_kind = {st.ordinal: lc.column_refs(st, known) for st in sts[5:]}
    ins, upd, alt, join, single, comma, dele = (by_kind[o] for o in range(6, 13))
    assert ins.writes == [("users", "id", 1.0), ("users", "email", 1.0)] and ins.reads == []
    assert upd.writes == [("users", "email", 1.0), ("users", "name", 1.0)]
    assert upd.reads == [("users", "id", 0.9)]
    assert alt.writes == [("users", "email", 1.0)] and alt.reads == [("users", "id", 0.9)]
    assert {(t, c) for t, c, _ in join.reads} == {
        ("users", "email"),
        ("orders", "total"),
        ("orders", "user_id"),
        ("users", "id"),
        ("users", "created"),
    }
    assert all(k == lc.CONF_QUALIFIED for _, _, k in join.reads)
    assert {(t, c) for t, c, _ in single.reads} == {("users", "email"), ("users", "id")}
    assert all(k == lc.CONF_SINGLE_TABLE for _, _, k in single.reads)
    # comma-joined list: the qualified references bind through both aliases
    assert {(t, c) for t, c, _ in comma.reads} == {
        ("users", "id"),
        ("orders", "user_id"),
        ("orders", "total"),
    }
    assert dele.reads == [("orders", "total", 0.9)]
    # a table named `update`: the mutation is the UPDATE that a space follows
    sts, known = _known(
        "CREATE TABLE `update` (email String);\nALTER TABLE `update` UPDATE email = 'z' WHERE 1;"
    )
    assert lc.column_refs(sts[1], known).writes == [("update", "email", 1.0)]


def test_nothing_binds_where_the_text_is_ambiguous() -> None:
    sts, known = _known(
        DDL
        + """
SELECT * FROM nobody_defines_me;
SELECT id FROM users, orders;
SELECT email FROM users WHERE id IN (SELECT user_id FROM orders);
INSERT INTO orders SELECT number, number, 1 FROM numbers(3);
DELETE FROM users;
SELECT id FROM nobody_defines_me WHERE x = 1;
"""
    )
    for st in sts[5:]:
        cr = lc.column_refs(st, known)
        assert cr.reads == [] and cr.writes == [], (st.body, cr)
    # the comma join is two tables to the table pass too
    assert references(sts[6]).reads == ["users", "orders"]
    # … and an INSERT … SELECT binds the SELECT's columns to its ONE source
    st = split_statements("INSERT INTO orders SELECT id, id, 1 FROM users WHERE email != ''")[0]
    assert lc.column_refs(st, known).reads == [("users", "id", 0.9), ("users", "email", 0.9)]


def test_alter_update_and_array_join_are_not_table_references() -> None:
    st = split_statements("ALTER TABLE users UPDATE email = 'z' WHERE id = 2")[0]
    assert references(st).writes == ["users"]
    st = split_statements("SELECT n.a FROM t_nested ARRAY JOIN n")[0]
    assert references(st).reads == ["t_nested"]


def test_hypotheses_name_the_columns_the_namespace_may_confirm() -> None:
    sts, known = _known("SELECT x, y FROM other_table WHERE z > 1 ORDER BY x")
    assert lc.column_hypotheses(sts, known) == {"other_table.x", "other_table.y", "other_table.z"}
    table = {
        "other_table.x": [("id-x", "mig::other_table.x", "column")],
        "other_table.y": [
            ("id-y1", "a::other_table.y", "column"),
            ("id-y2", "b::other_table.y", "column"),
        ],
        "other_table.z": [("id-z", "mig::other_table.z", "table")],
    }
    confirmed = lc.known_from_name_table(table, lc.column_hypotheses(sts, known))
    assert confirmed == {"other_table": {"x"}}  # unique column candidates only
    merged = lc.merge_known(known, confirmed)
    assert lc.column_refs(sts[0], merged).reads == [("other_table", "x", 0.9)]


def test_symbols_facets_and_edges() -> None:
    sts, known = _known(
        DDL + "SELECT email FROM users WHERE id = 1;\nINSERT INTO users (id) VALUES (2);"
    )
    syms = lc.column_symbols("db/schema.sql", "db.schema", sts)
    assert {s.kind for s in syms} == {"column"}
    assert {s.name for s in syms} >= {"users.id", "users.email", "orders.total", "users.mail"}
    assert (
        next(s for s in syms if s.name == "users.email").qualified_name == "db.schema::users.email"
    )
    facets = lc.column_facets(sts, known)
    assert {(f.kind, f.name) for f in facets} >= {
        ("column_read", "users.email"),
        ("column_read", "users.id"),
        ("column_write", "users.id"),
        ("column_write", "users.created"),  # the DROP COLUMN
    }
    edges = lc.column_edges("db/schema.sql", "db.schema", sts, known, syms, {}, rw_kinds=True)
    kinds = {(e.edge_kind, e.to_qualified_name) for e in edges}
    assert ("reads", "db.schema::users.email") in kinds and (
        "writes",
        "db.schema::users.id",
    ) in kinds
    assert all(e.confidence in (0.9, 1.0) for e in edges)  # own-file scale 1.0
    # a column another file defines binds through the name table at 0.9 ×
    other = split_statements("SELECT o.total FROM orders o")
    table = {"orders.total": [("id-t", "mig::orders.total", "column")]}
    e2 = lc.column_edges("q.sql", "q", other, {}, [], table, rw_kinds=True)
    assert [(e.edge_kind, e.to_symbol_id, e.confidence) for e in e2] == [("reads", "id-t", 0.855)]


@pytest.mark.parametrize("flag", [False, True])
def test_gate_wires_the_sql_and_python_lanes(monkeypatch: pytest.MonkeyPatch, flag: bool) -> None:
    from server.code.app.index.ingest_parse import parse_file
    from server.code.domain.extract.lang_sql import sql_units
    from server.settings import settings

    monkeypatch.setattr(settings, "code_sql_column_lineage", flag, raising=False)
    monkeypatch.setattr(settings, "code_sql_statement_records", True, raising=False)
    monkeypatch.setattr(settings, "code_py_sql_islands", True, raising=False)
    pf = parse_file("db/t.sql", b"CREATE TABLE t (a Int, b Int);\nSELECT a FROM t WHERE b = 1;\n")
    assert pf is not None
    assert (sum(s.kind == "column" for s in pf.symbols) == 2) is flag
    units = sql_units(pf)
    assert any(f.kind == "column_read" for f in units.facets) is flag
    py = parse_file(
        "tests/integration/test_x/test.py",
        b'def test(node):\n    node.query("CREATE TABLE t (a Int, b Int)")\n',
    )
    assert py is not None and (sum(s.kind == "column" for s in py.symbols) == 2) is flag


_NEST = "WITH a AS ({}SELECT 1{}"


@pytest.mark.parametrize(
    "unit", [",a AS (", "INSERT INTO a(", ",a AS (SELECT 1", ",FROM a", "SELECT 1, a AS ("]
)
def test_column_refs_on_a_long_run_costs_linear_time(unit: str) -> None:
    def text(n: int) -> str:  # the nested scopes closed; every other run left open
        return _NEST.format(unit * n, ")" * (n + 1)) if unit.startswith("SELECT") else unit * n

    def cost(n: int) -> float:
        (st,) = split_statements(text(n))
        t0 = time.perf_counter()
        lc.column_refs(st, {"a": {"x"}})
        return time.perf_counter() - t0

    assert_linear(cost, 2_000, 8_000, floor=0.05, bound=2.0)


@pytest.mark.parametrize("width", [lc.STAR_COLUMNS_MAX, lc.STAR_COLUMNS_MAX + 1])
def test_a_star_reads_a_table_of_at_most_the_cap_of_columns(width: int) -> None:
    cols = ", ".join(f"c{i:04} Int" for i in range(width))
    sts, known = _known(f"CREATE TABLE w ({cols});\nSELECT * FROM w;")
    reads = [c for _, c, _ in lc.column_refs(sts[1], known).reads]
    assert reads == ([f"c{i:04}" for i in range(width)] if width <= lc.STAR_COLUMNS_MAX else [])


@pytest.mark.parametrize("width", [lc.STAR_COLUMNS_MAX, 15_000])
def test_many_stars_over_a_wide_table_cost_linear_time(width: int) -> None:
    cols = ", ".join(f"c{i} Int" for i in range(width))

    def cost(n: int) -> float:
        sts, known = _known(f"CREATE TABLE w ({cols});\n" + "SELECT * FROM w;\n" * n)
        t0 = time.perf_counter()
        facets = lc.column_facets(sts, known)
        took = time.perf_counter() - t0
        assert len(facets) <= lc.STAR_READS_MAX
        return took

    assert_linear(cost, 500, 2_000, floor=0.05, bound=2.0)
