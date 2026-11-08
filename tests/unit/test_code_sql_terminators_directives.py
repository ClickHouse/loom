"""The splitter's terminator modes and the migration / query
tools' directive table."""

from __future__ import annotations

import time

import pytest

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_sql as ls
from server.code.domain.extract import lang_sql_directives as ld
from server.code.domain.extract import lang_sql_split
from tests.unit.code_fixtures import assert_linear

_PLPGSQL = """\
CREATE FUNCTION f() RETURNS int AS $$
BEGIN
  RETURN 1; -- inner semicolons are the body's
END;
$$ LANGUAGE plpgsql;
SELECT f();
CREATE FUNCTION g() RETURNS void AS $body$ BEGIN PERFORM 1; END; $body$ LANGUAGE plpgsql;
"""

_TSQL = """\
CREATE TABLE a (x int)
GO
INSERT INTO a VALUES (1); INSERT INTO a VALUES (2)
GO 2
SELECT 1
"""

_MYSQL = """\
DELIMITER $$
CREATE PROCEDURE p()
BEGIN
  SELECT 1;
  SELECT 2;
END$$
DELIMITER ;
# a MySQL comment; with a semicolon
SELECT 3;
"""

_PSQL = """\
\\set ON_ERROR_STOP 1
\\ir ../common/setup.sql
\\i /abs/path.sql
SELECT 1;
CREATE OR REPLACE FUNCTION g() RETURNS void AS $b$ BEGIN END; $b$ LANGUAGE plpgsql
/
"""

_GOOSE = """\
-- +goose Up
-- +goose StatementBegin
CREATE TRIGGER t BEFORE INSERT ON x FOR EACH ROW BEGIN SET NEW.a = 1; END;
-- +goose StatementEnd
CREATE INDEX i ON x(a);

-- +goose Down
DROP INDEX i;
"""

_DBMATE = """\
-- migrate:up
CREATE TABLE users (id int);
-- migrate:down
DROP TABLE users;
"""

_LIQUIBASE = """\
--liquibase formatted sql

--changeset alice:1 runInTransaction:false
CREATE TABLE t (id int);
--rollback DROP TABLE t;

--changeset bob:2
ALTER TABLE t ADD COLUMN name text;
"""

_SQLC = """\
-- name: GetAuthor :one
SELECT * FROM authors WHERE id = $1 LIMIT 1;

-- name: ListAuthors :many
SELECT * FROM authors ORDER BY name;

-- name: DeleteAuthor :exec
DELETE FROM authors WHERE id = $1;
"""

_SQITCH = """\
-- Deploy flipr:users to pg
-- requires: appschema
BEGIN;
CREATE TABLE users (nick text);
COMMIT;
"""


def _kinds(text: str) -> list[tuple[str, int, int]]:
    return [(s.kind, s.start_line, s.end_line) for s in lang_sql_split.split_statements(text)]


def test_dollar_quoted_bodies_are_one_statement() -> None:
    assert _kinds(_PLPGSQL) == [
        ("CREATE FUNCTION", 1, 5),
        ("SELECT", 6, 6),
        ("CREATE FUNCTION", 7, 7),
    ]
    # ClickHouse heredoc strings use the same quoting and split the same way
    assert _kinds("SELECT $$a;b$$; SELECT 2;") == [("SELECT", 1, 1), ("SELECT", 1, 1)]


def test_go_batches_slash_and_delimiter_terminators() -> None:
    mode = lang_sql_split.detect_mode(_TSQL)
    assert mode.go_batches and not mode.mysql_delimiter
    assert _kinds(_TSQL) == [
        ("CREATE TABLE", 1, 2),
        ("INSERT", 3, 3),
        ("INSERT", 3, 4),
        ("SELECT", 5, 5),
    ]
    assert (
        lang_sql_split.detect_mode(_MYSQL).mysql_delimiter
        and lang_sql_split.detect_mode(_MYSQL).hash_comments
    )
    assert _kinds(_MYSQL) == [
        ("DELIMITER", 1, 1),
        ("CREATE PROCEDURE", 2, 6),
        ("DELIMITER", 7, 7),
        ("SELECT", 8, 9),
    ]
    st = lang_sql_split.split_statements(_MYSQL)[3]
    assert st.body == "SELECT 3;" and st.comments == "# a MySQL comment; with a semicolon"
    assert (
        lang_sql_split.detect_mode(_PSQL).psql_meta
        and lang_sql_split.detect_mode(_PSQL).slash_terminator
    )
    assert _kinds(_PSQL) == [
        ("\\set", 1, 1),
        ("\\ir", 2, 2),
        ("\\i", 3, 3),
        ("SELECT", 4, 4),
        ("CREATE FUNCTION", 5, 6),
    ]


def test_goose_blocks_keep_their_statement_whole() -> None:
    assert lang_sql_split.detect_mode(_GOOSE).statement_blocks == (
        lang_sql_split.GOOSE_STATEMENT_BLOCK,
    )
    sts = lang_sql_split.split_statements(_GOOSE)
    assert [(s.kind, s.start_line, s.end_line) for s in sts] == [
        ("CREATE TRIGGER", 1, 4),
        ("CREATE INDEX", 5, 5),
        ("DROP INDEX", 7, 8),
    ]
    assert "-- +goose Up" in sts[0].comments and "-- +goose Down" in sts[2].comments


def test_directives_name_tool_direction_and_version() -> None:
    du = ld.directive_units(
        "db/migrations/00003_add_index.sql", _GOOSE, lang_sql_split.split_statements(_GOOSE)
    )
    assert (du.tool, du.version) == ("goose", "00003")
    assert du.directions == {1: "up", 2: "up", 3: "down"}
    kinds = {(f.kind, f.name, f.ordinal) for f in du.facets}
    assert ("tool", "goose", 0) in kinds and ("migration", "00003", 0) in kinds
    assert ("direction", "up", 1) in kinds and ("direction", "down", 3) in kinds
    du = ld.directive_units(
        "db/migrate/20240101120000_users.sql", _DBMATE, lang_sql_split.split_statements(_DBMATE)
    )
    assert (du.tool, du.version, du.directions) == (
        "dbmate",
        "20240101120000",
        {1: "up", 2: "down"},
    )
    du = ld.directive_units(
        "sql/V1_2__users.sql",
        "CREATE TABLE u (id int);",
        lang_sql_split.split_statements("CREATE TABLE u (id int);"),
    )
    assert (du.tool, du.version, du.directions) == ("flyway", "1_2", {1: "up"})
    du = ld.directive_units(
        "sql/U1_2__users.sql", "DROP TABLE u;", lang_sql_split.split_statements("DROP TABLE u;")
    )
    assert (du.tool, du.directions) == ("flyway", {1: "undo"})
    du = ld.directive_units(
        "prisma/migrations/20240101120000_init/migration.sql",
        "CREATE TABLE a (x int);",
        lang_sql_split.split_statements("CREATE TABLE a (x int);"),
    )
    assert (du.tool, du.version, du.directions) == ("prisma", "20240101120000", {1: "up"})
    du = ld.directive_units("deploy/users.sql", _SQITCH, lang_sql_split.split_statements(_SQITCH))
    assert du.tool == "sqitch" and set(du.directions.values()) == {"deploy"}
    du = ld.directive_units(
        "tests/queries/0_stateless/00001_x.sql",
        "-- Tags: no-fasttest\nSELECT 1;",
        lang_sql_split.split_statements("-- Tags: no-fasttest\nSELECT 1;"),
    )
    assert du.tool == "clickhouse-test" and du.facets == [] and du.prefix_parts() == []


def test_changesets_and_named_queries_are_symbols() -> None:
    sts = lang_sql_split.split_statements(_LIQUIBASE)
    du = ld.directive_units("db/changelog/001.sql", _LIQUIBASE, sts)
    assert du.tool == "liquibase"
    assert [(s.name, s.kind, s.start_line, s.end_line) for s in du.symbols] == [
        ("alice:1", "changeset", 3, 5),
        ("bob:2", "changeset", 7, 8),
    ]
    assert du.symbols[0].signature == "changeset alice:1 runInTransaction:false"
    assert ("direction", "rollback", 1, 5) in {
        (f.kind, f.name, f.ordinal, f.line) for f in du.facets
    }
    sts = lang_sql_split.split_statements(_SQLC)
    du = ld.directive_units("db/query/authors.sql", _SQLC, sts)
    assert du.tool == "sqlc"
    assert [(s.name, s.kind, s.start_line, s.end_line, s.signature) for s in du.symbols] == [
        ("GetAuthor", "query", 1, 2, "GetAuthor :one · SELECT"),
        ("ListAuthors", "query", 4, 5, "ListAuthors :many · SELECT"),
        ("DeleteAuthor", "query", 7, 8, "DeleteAuthor :exec · DELETE"),
    ]
    assert du.symbols[0].qualified_name == "db.query.authors::GetAuthor"


def test_psql_includes_resolve_against_the_file() -> None:
    assert ld.psql_includes("sql/updates/2.1.sql", _PSQL) == ["sql/common/setup.sql"]
    assert ld.psql_includes("x.sql", "\\ir :variable\n\\i $HOME/a.sql\n") == []


def test_parse_and_units_carry_the_directive_rows(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_sql_statement_records", True, raising=False)
    pf = parse_file("db/query/authors.sql", _SQLC.encode())
    assert pf is not None and pf.lang == "sql"
    assert [s.kind for s in pf.symbols] == ["statement"] * 3 + ["query"] * 3
    pf = parse_file("sql/updates/2.1.sql", _PSQL.encode())
    assert pf is not None and pf.includes == ["sql/common/setup.sql"]
    pf = parse_file("db/migrations/00003_add_index.sql", _GOOSE.encode())
    assert pf is not None
    units = ls.sql_units(pf)
    assert {(f.kind, f.name) for f in units.facets} >= {
        ("tool", "goose"),
        ("migration", "00003"),
        ("direction", "up"),
        ("direction", "down"),
        ("statement_kind", "CREATE TRIGGER"),
    }
    assert units.prefixes[0].startswith("00003_add_index.sql · goose 00003 · down, up · ")


@pytest.mark.parametrize(
    "head,unit",
    [
        ("", "--changeset a:1\nSELECT 1;\n--rollback x\n"),
        ("", "-- name: Q :one\nSELECT 1;\n"),
        ("", "CREATE TABLE a{} (x Int);\nSELECT 1 FROM a{};\n"),
        ("", "\n"),
        ("DELIMITER --\nSELECT 1--", "\n--"),  # each `--` ends a comment-only tail
    ],
)
def test_a_file_of_many_directives_or_definitions_parses_in_linear_time(
    head: str, unit: str
) -> None:
    def cost(n: int) -> float:
        text = head + "".join(unit.format(i, i) for i in range(n))
        t0 = time.perf_counter()
        ls.parse_sql("db/m.sql", text.encode())
        return time.perf_counter() - t0

    assert_linear(cost, 4_000, 16_000)


def test_a_comment_after_the_last_statement_ends_it_on_its_own_line() -> None:
    for tail in ("-- tail\n", "-- tail\n  "):
        sts = lang_sql_split.split_statements("SELECT 1;\n\nSELECT 2;\n" + tail)
        assert (sts[-1].start_line, sts[-1].end_line) == (3, 4), tail
