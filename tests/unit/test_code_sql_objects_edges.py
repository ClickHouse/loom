"""The objects a SQL file creates, the tables its statements
read and write, and the edges between them."""

from __future__ import annotations

import time

import pytest

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_sql as ls
from server.code.domain.extract import lang_sql_objects as lo
from server.code.domain.extract import lang_sql_split
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.name_scope import gate_name_table
from tests.unit.code_fixtures import assert_linear

_TEST = b"""\
-- Tags: no-fasttest
DROP TABLE IF EXISTS t;
CREATE TABLE t (x Int32) ENGINE = MergeTree ORDER BY x;
CREATE MATERIALIZED VIEW mv TO t_agg AS SELECT count() FROM t;
INSERT INTO t SELECT number FROM numbers(10);
WITH c AS (SELECT x FROM t) SELECT * FROM c JOIN system.parts ON 1 = 1;
SELECT count() FROM {CLICKHOUSE_DATABASE:Identifier}.t FINAL;
ALTER TABLE t ADD COLUMN y Int32;
OPTIMIZE TABLE t FINAL;
SHOW CREATE TABLE t;
DROP TABLE t;
"""

_SCHEMA = b"""\
CREATE TABLE authors (id BIGSERIAL PRIMARY KEY, name text NOT NULL, bio text);
CREATE INDEX authors_name_idx ON authors (name);
CREATE OR REPLACE FUNCTION touch_author(aid bigint) RETURNS void AS $$
BEGIN UPDATE authors SET bio = bio WHERE id = aid; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER authors_touch AFTER UPDATE ON authors FOR EACH ROW EXECUTE FUNCTION touch_author(NEW.id);
"""

_QUERIES = b"""\
-- name: GetAuthor :one
SELECT * FROM authors WHERE id = $1 LIMIT 1;

-- name: TouchAuthor :exec
SELECT touch_author($1);

-- name: ListFromNowhere :many
SELECT * FROM somewhere_else;
"""


def test_created_objects_and_references_of_a_test_file() -> None:
    sts = lang_sql_split.split_statements(_TEST.decode())
    objs = [
        (s.name, s.kind, s.start_line, s.end_line)
        for s in lo.object_symbols("tests/queries/0_stateless/03000_t.sql", "m", sts)
    ]
    assert objs == [("t", "table", 3, 3), ("mv", "view", 4, 4)]
    refs = {st.ordinal: lo.references(st) for st in sts}
    assert refs[1].writes == ["t"] and refs[1].creates is None
    assert refs[2].creates is not None and (refs[2].creates.kind, refs[2].creates.bare) == (
        "table",
        "t",
    )
    assert refs[2].reads == [] and refs[2].writes == []  # the object being defined is no reference
    assert refs[3].creates is not None and refs[3].creates.kind == "view"
    assert (refs[3].reads, refs[3].writes) == (
        ["t"],
        ["t_agg"],
    )  # MV: reads its source, writes its target
    assert refs[4].writes == ["t"] and refs[4].reads == []  # numbers(10) is a table function
    assert refs[5].reads == ["t"]  # the CTE `c` and system.parts are not tables of the tree
    assert refs[6].reads == ["{CLICKHOUSE_DATABASE:Identifier}.t"]
    assert lo.bare_name(refs[6].reads[0]) == "t"
    assert refs[7].writes == ["t"] and refs[8].writes == ["t"]
    assert refs[9].reads == ["t"] and refs[10].writes == ["t"]
    facets = [(f.kind, f.name, f.ordinal) for f in lo.reference_facets(sts)]
    assert ("creates", "t", 2) in facets and ("creates", "mv", 3) in facets
    assert ("table_write", "t_agg", 3) in facets and ("table_read", "t", 5) in facets
    assert not any(n in ("c", "parts", "numbers", "number") for _, n, _ in facets)


def test_edges_bind_to_the_files_own_definitions_first_then_repository_unique() -> None:
    path, mod = (
        "tests/queries/0_stateless/03000_t.sql",
        module_scope("tests/queries/0_stateless/03000_t.sql"),
    )
    sts = lang_sql_split.split_statements(_TEST.decode())
    own = lo.object_symbols(path, mod, sts)
    # another test's `t` and a repository-unique `t_agg` in the name table
    other_t = ("id-other-t", "tests.queries.0_stateless.03001_t::t", "table")
    table = {
        "t": [other_t, (own[0].id, own[0].qualified_name, "table")],
        "t_agg": [("id-agg", "sql.schema::t_agg", "table")],
        "count": [("id-count", "src.count", "function")],
    }
    edges = lo.sql_edges(path, mod, sts, own, gate_name_table(table, "sql"))
    by_from = {}
    for e in edges:
        by_from.setdefault(e.from_qualified_name.rsplit("::", 1)[1], []).append(
            (e.to_qualified_name, e.confidence, e.edge_kind)
        )
    t_own = f"{mod}::t"
    assert by_from["stmt_0001"] == [
        (t_own, 1.0, "calls")
    ]  # DROP TABLE t → its own t, not the other test's
    assert by_from["stmt_0003"] == [("sql.schema::t_agg", 0.9, "calls"), (t_own, 1.0, "calls")]
    assert by_from["stmt_0005"] == [(t_own, 1.0, "calls")]  # count() is no SQL object
    assert "stmt_0002" not in by_from  # CREATE TABLE t defines t; no edge to itself
    assert all(e.line for e in edges)


def test_repository_unique_binding_declines_ambiguity_and_wrong_kinds() -> None:
    assert (
        lo._unique([("a", "x::t", "table"), ("b", "y::t", "table")], lo.CALLABLE_KINDS | {"table"})
        is None
    )
    assert lo._unique([("a", "x::t", "table"), ("a2", "x::t", "table")], frozenset({"table"})) == (
        "a",
        "x::t",
    )
    assert lo._unique([("a", "x::t", "function")], frozenset({"table"})) is None


def test_name_gate_keeps_sql_objects_for_sql_callers_only() -> None:
    table = {
        "users": [("id1", "db.schema::users", "table"), ("id2", "app.models::users", "function")]
    }
    assert gate_name_table(table, "python") == {"users": [("id2", "app.models::users", "function")]}
    assert gate_name_table(table, "sql") == table  # a SQL caller may bind either
    assert gate_name_table({"t": [("id", "x::t", "table")]}, "cpp") == {}


def test_schema_and_queries_end_to_end_through_parse_file(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_sql_statement_records", True, raising=False)
    schema = parse_file("db/schema.sql", _SCHEMA)
    assert schema is not None
    kinds = {s.name: s.kind for s in schema.symbols if s.kind != "statement"}
    assert kinds == {
        "authors": "table",
        "authors_name_idx": "index",
        "touch_author": "function",
        "authors_touch": "trigger",
    }
    sts = lang_sql_split.split_statements(_SCHEMA.decode())
    assert lo.references(sts[1]).writes == ["authors"]  # CREATE INDEX … ON authors
    assert lo.references(sts[3]).writes == ["authors"]  # CREATE TRIGGER … ON authors
    assert lo.references(sts[2]).writes == ["authors"]  # the function body's UPDATE
    queries = parse_file("db/query/authors.sql", _QUERIES)
    assert queries is not None
    q_sts = lang_sql_split.split_statements(_QUERIES.decode())
    table = {
        s.name: [(s.id, s.qualified_name, s.kind)] for s in schema.symbols if s.kind != "statement"
    }
    edges = lo.sql_edges(
        "db/query/authors.sql",
        module_scope("db/query/authors.sql"),
        q_sts,
        queries.symbols,
        gate_name_table(table, "sql"),
    )
    targets = sorted(
        (e.from_qualified_name.rsplit("::", 1)[1], e.to_qualified_name.rsplit("::", 1)[1])
        for e in edges
    )
    assert targets == [("stmt_0001", "authors"), ("stmt_0002", "touch_author")]
    # `somewhere_else` is created nowhere in the tree: no edge and no miss
    units = ls.sql_units(queries)
    assert units.prefixes[0].endswith("reads: authors, somewhere_else")
    assert ("table_read", "somewhere_else") in {(f.kind, f.name) for f in units.facets}


def test_rw_kinds_split_table_references_into_reads_and_writes() -> None:
    """With `rw_kinds` a statement's table references are `writes` / `reads`
    edges; functions stay `calls`; off, everything is `calls`."""
    path, mod = (
        "tests/queries/0_stateless/03000_t.sql",
        module_scope("tests/queries/0_stateless/03000_t.sql"),
    )
    sts = lang_sql_split.split_statements(_TEST.decode())
    own = lo.object_symbols(path, mod, sts)
    table = {"t_agg": [("id-agg", "sql.schema::t_agg", "table")]}
    on = lo.sql_edges(path, mod, sts, own, gate_name_table(table, "sql"), rw_kinds=True)
    kinds = {
        (
            e.from_qualified_name.rsplit("::", 1)[1],
            e.to_qualified_name.rsplit("::", 1)[1],
            e.edge_kind,
        )
        for e in on
    }
    assert ("stmt_0001", "t", "writes") in kinds  # DROP TABLE t
    assert ("stmt_0003", "t", "reads") in kinds and ("stmt_0003", "t_agg", "writes") in kinds  # MV
    assert ("stmt_0005", "t", "reads") in kinds  # WITH … SELECT FROM t
    assert ("stmt_0008", "t", "writes") in kinds  # OPTIMIZE TABLE t
    assert not any(k == "calls" for _, _, k in kinds if _ != "count")
    off = lo.sql_edges(path, mod, sts, own, gate_name_table(table, "sql"))
    assert {e.edge_kind for e in off} == {"calls"} and len(off) == len(on)


def test_rw_kinds_make_a_dbt_ref_a_read() -> None:
    from server.code.domain.extract import lang_sql_jinja as lj

    model = lj.jinja_units("models/a.sql", "models.a", "select * from {{ ref('b') }}")
    table = {"b": [("id-b", "models.b::b", "model")]}
    assert [e.edge_kind for e in lj.jinja_edges("models/a.sql", model, table)] == ["calls"]
    assert [e.edge_kind for e in lj.jinja_edges("models/a.sql", model, table, rw_kinds=True)] == [
        "reads"
    ]


# ---- schema-aware, directory-scoped resolution ----------------------------


def test_schema_contexts_and_qualified_symbol_names() -> None:
    text = (
        "CREATE TABLE t0 (x int);\nUSE shop;\nCREATE TABLE users (id int);\n"
        "SET search_path TO analytics, public;\nCREATE VIEW v AS SELECT 1;\n"
        "CREATE TABLE billing.invoices (id int);\nCREATE SCHEMA audit;\n"
    )
    sts = lang_sql_split.split_statements(text)
    assert lo.schema_contexts(sts) == {
        1: "",
        2: "shop",
        3: "shop",
        4: "analytics",
        5: "analytics",
        6: "analytics",
        7: "analytics",
    }
    syms = {s.name: s.qualified_name for s in lo.object_symbols("db/schema.sql", "db.schema", sts)}
    assert syms == {
        "t0": "db.schema::t0",
        "users": "db.schema::shop.users",
        "v": "db.schema::analytics.v",
        "invoices": "db.schema::billing.invoices",
        "audit": "db.schema::audit",
    }
    assert lo.split_schema("{CLICKHOUSE_DATABASE:Identifier}.t") == ("", "t")
    assert lo.split_schema('public."Users"') == ("public", "Users")
    assert lo.split_schema("t") == ("", "t")


def _cand(mod: str, tail: str, kind: str = "table") -> tuple[str, str, str]:
    return (f"id:{mod}::{tail}", f"{mod}::{tail}", kind)


def test_resolution_ladder_prefers_written_schema_then_nearest_directory() -> None:
    table = {
        "authors": [
            _cand("examples.authors.postgresql.schema", "authors"),
            _cand("examples.authors.mysql.schema", "authors"),
            _cand("examples.booktest.postgresql.schema", "authors"),
        ],
        "users": [
            _cand("db.core", "public.users"),
            _cand("db.legacy", "old.users"),
        ],
        "orders": [_cand("a.b.c", "orders")],
    }
    K = lo.SQL_OBJECT_KINDS
    # nearest directory: the query file next to its own schema.sql wins
    hit = lo.resolve_object("authors", "", K, [], table, "examples/authors/postgresql/query.sql")
    assert hit == (
        "id:examples.authors.postgresql.schema::authors",
        "examples.authors.postgresql.schema::authors",
        0.95,
    )
    # a file above the candidates ties at depth 1 between three definitions: nothing binds
    assert lo.resolve_object("authors", "", K, [], table, "examples/readme.sql") is None
    # the written schema chooses among candidates in different schemas
    hit = lo.resolve_object("users", "old", K, [], table, "db/x.sql")
    assert hit is not None and hit[1] == "db.legacy::old.users"
    assert (
        lo.resolve_object("users", "", K, [], table, "db/x.sql") is None
    )  # two schemas, unqualified
    # repository-unique from anywhere, at 0.9
    assert lo.resolve_object("orders", "", K, [], table, "elsewhere/q.sql") == (
        "id:a.b.c::orders",
        "a.b.c::orders",
        0.9,
    )
    # own file first, at 1.0, and the same schema first when qualified
    sts = lang_sql_split.split_statements(
        "CREATE TABLE s1.users (id int);\nCREATE TABLE s2.users (id int);"
    )
    own = lo.object_symbols("db/x.sql", "db.x", sts)
    hit = lo.resolve_object("users", "s2", K, own, table, "db/x.sql")
    assert hit is not None and hit[1] == "db.x::s2.users" and hit[2] == 1.0


def test_edges_sit_on_the_reference_line_inside_a_body() -> None:
    text = (
        "CREATE TABLE authors (id int);\n"
        "CREATE FUNCTION touch() RETURNS void AS $$\nBEGIN\n  UPDATE authors SET id = id;\n"
        "  DELETE FROM drafts;\nEND;\n$$ LANGUAGE plpgsql;\n"
    )
    sts = lang_sql_split.split_statements(text)
    own = lo.object_symbols("s.sql", "s", sts)
    edges = lo.sql_edges("s.sql", "s", sts, own, {}, rw_kinds=True)
    assert [(e.edge_kind, e.line) for e in edges] == [
        ("writes", 4)
    ]  # the UPDATE's line, not the CREATE's
    facets = {(f.kind, f.name, f.line) for f in lo.reference_facets(sts)}
    assert ("table_write", "authors", 4) in facets and ("table_write", "drafts", 5) in facets


@pytest.mark.parametrize("name", ["a{}", "a${}", "`a {}`"])
def test_reference_lines_of_a_statement_of_many_names_cost_linear_time(name: str) -> None:
    def cost(n: int) -> float:
        names = ",\n".join(name.format(i) for i in range(n))
        sts = lang_sql_split.split_statements("SELECT 1 FROM " + names)
        # The parser's own CPU time: a loaded runner's other processes do not count.
        t0 = time.process_time()
        facets = lo.reference_facets(sts)
        took = time.process_time() - t0
        assert {f.line for f in facets} == set(range(1, n + 1))
        return took

    assert_linear(cost, 2_000, 8_000, floor=0.05)


def test_a_quoted_name_after_an_escaped_quote_in_a_literal_is_found() -> None:
    from server.code.domain.extract.lang_sql_facets import line_of_name

    (st,) = lang_sql_split.split_statements("SELECT 1\nFROM 'a\\'b', \"my-col\", 'x'")
    assert line_of_name(st, "my-col") == 2


def test_edges_of_one_name_defined_in_many_schemas_cost_linear_time() -> None:
    def cost(n: int) -> float:
        text = "".join(f"CREATE TABLE s{i}.a (x Int);\nSELECT 1 FROM a;\n" for i in range(n))
        sts = lang_sql_split.split_statements(text)
        own = lo.object_symbols("m.sql", "m", sts)
        t0 = time.perf_counter()
        lo.sql_edges("m.sql", "m", sts, own, {})
        return time.perf_counter() - t0

    assert_linear(cost, 4_000, 16_000)


def test_a_quoted_name_after_a_literal_over_two_lines_is_found() -> None:
    from server.code.domain.extract.lang_sql_facets import line_of_name

    (st,) = lang_sql_split.split_statements("SELECT 'line one\nline two' FROM \"my t\"")
    assert line_of_name(st, "my t") == 2
