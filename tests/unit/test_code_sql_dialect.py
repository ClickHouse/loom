"""A SQL file's dialect from its own text."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_sql as ls
from server.code.domain.extract import lang_sql_dialect as ld


def test_each_dialects_own_syntax_is_recognised_clearly() -> None:
    cases = {
        "clickhouse": "CREATE TABLE t (x UInt32) ENGINE = MergeTree ORDER BY x SETTINGS index_granularity = 8192;\n"
        "SELECT * FROM t FORMAT TSV;",
        "postgres": "CREATE FUNCTION f() RETURNS TRIGGER AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql;\n"
        "CREATE TABLE u (id SERIAL PRIMARY KEY, d JSONB);",
        "mysql": "CREATE TABLE `t` (`id` int UNSIGNED AUTO_INCREMENT) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
        "tsql": "CREATE TABLE [dbo].[t] (id INT IDENTITY(1,1), n NVARCHAR(50))\nGO\nSELECT TOP 10 * FROM dbo.t\nGO\n",
        "oracle": "CREATE TABLE t (id NUMBER(10), n VARCHAR2(50));\nSELECT SYSDATE FROM DUAL;\n/\n",
        "sqlite": "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT);\nPRAGMA foreign_keys = ON;",
        "bigquery": "CREATE TABLE `proj.ds.t` (a STRUCT<x INT64>, b ARRAY<STRING>);",
        "snowflake": "CREATE OR REPLACE STAGE s;\nSELECT v:name FROM t, LATERAL FLATTEN(input => v) WAREHOUSE = w;",
        "duckdb": "INSTALL httpfs;\nSELECT * FROM read_parquet('x.parquet');",
        "sparksql": "CREATE TABLE t (a ARRAY<STRUCT<x: INT>>) USING iceberg TBLPROPERTIES ('k' = 'v');",
    }
    for dialect, text in cases.items():
        got, conf = ld.detect_dialect(text)
        assert (got, conf) == (dialect, ld.CONF_CLEAR), (
            dialect,
            got,
            conf,
            ld.dialect_scores(text),
        )


def test_plain_sql_and_thin_leads() -> None:
    assert ld.detect_dialect("SELECT id, name FROM users WHERE id = 1;") == ("", 0.0)
    assert ld.detect_dialect("CREATE TABLE t (id INTEGER PRIMARY KEY);") == ("", 0.0)  # everybody's
    # one strong idiom alone leans (weight 2), three weights' worth decides
    got, conf = ld.detect_dialect("SELECT 1::int;")
    assert got == "postgres" and conf == ld.CONF_LEAN
    got, conf = ld.detect_dialect("INSERT INTO t VALUES (1) RETURNING id;\nSELECT 1::int;")
    assert got == "postgres" and conf == ld.CONF_CLEAR
    # ClickHouse's Int64 is not BigQuery's INT64
    got, _ = ld.detect_dialect("CREATE TABLE t (x Int64, s String) ENGINE = Memory;")
    assert got == "clickhouse"


def test_sql_units_carry_one_file_level_dialect_facet(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_sql_statement_records", True, raising=False)
    pf = parse_file(
        "db/schema.sql", b"CREATE TABLE u (id SERIAL PRIMARY KEY);\nCREATE EXTENSION pgcrypto;\n"
    )
    assert pf is not None
    units = ls.sql_units(pf)
    dialect = [f for f in units.facets if f.kind == "dialect"]
    assert [(f.name, f.ordinal, f.line, f.confidence) for f in dialect] == [("postgres", 0, 1, 0.9)]
    pf = parse_file("db/plain.sql", b"SELECT id FROM users;\n")
    assert pf is not None and not [f for f in ls.sql_units(pf).facets if f.kind == "dialect"]
