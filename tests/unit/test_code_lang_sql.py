"""A stateless SQL test is a sequence of statement records.

Splitter (the runner's `;`-at-depth-0 contract with the TestHint on the
statement's line), header (the runner's `Tags:` rule), hint grammar
(`src/Client/TestHint.cpp`), statement kinds, facets, one `statement`
symbol per record, statement-packed chunks that never cut a statement,
and the deterministic context prefix.
"""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_sql as ls
from server.code.domain.extract import lang_sql_facets, lang_sql_model, lang_sql_split
from server.code.domain.extract.treesitter import symbol_id

_SQL = b"""-- Tags: no-fasttest, zookeeper
-- Random settings limits: max_threads=(1, 4)

DROP TABLE IF EXISTS t;
-- the table under test
CREATE TABLE t (d DateTime('UTC'), s String) ENGINE = ReplicatedMergeTree('/t/{shard}', '{replica}') ORDER BY d;

INSERT INTO t(d, s) VALUES (toDateTime('2016-06-15 23:00:00'), 'a;b'); -- a ';' inside a string
SET dialect = 'clickhouse', max_threads = 2;
SELECT quantileExactWeightedInterpolated(0.2)(d, 1) FROM t SETTINGS optimize_read_in_order = 1 FORMAT TSV;
SELECT arrayJoin(); -- { serverError NUMBER_OF_ARGUMENTS_DOESNT_MATCH, BAD_ARGUMENTS }
SELECT /; -- { clientError 62 }
SELECT count() FROM system.query_log WHERE 1 /* { echoOn } */;
SYSTEM FLUSH LOGS query_log;
DROP TABLE t
"""


def _stmts() -> list[lang_sql_model.Statement]:
    return lang_sql_split.split_statements(_SQL.decode())


def test_desc_reads_a_table_only_where_a_statement_starts() -> None:
    from server.code.domain.extract.lang_sql_objects import references

    def reads(sql: str) -> list[str]:
        return references(lang_sql_split.split_statements(sql)[0]).reads

    assert reads("DESC t;") == ["t"] and reads("DESC TABLE t;") == ["t"]
    assert reads("DESCRIBE TABLE t FORMAT TSV;") == ["t"]
    # `ORDER BY x DESC LIMIT 1` must not read as a DESCRIBE of `LIMIT`
    assert reads("SELECT * FROM s ORDER BY s.event_ts DESC LIMIT 1 BY s.id;") == ["s"]


def test_header_follows_the_runner_rule() -> None:
    h = lang_sql_model.parse_header(_SQL.decode())
    assert h.tags == ("no-fasttest", "zookeeper")
    assert h.random_settings_limits == "max_threads=(1, 4)"
    assert h.memory_limits == ""
    # the sign must start the line, the key may be indented after it
    assert lang_sql_model.parse_header("--   Tags: long\nSELECT 1;").tags == ("long",)
    assert lang_sql_model.parse_header("  -- Tags: long\nSELECT 1;").tags == ()


def test_split_respects_strings_comments_and_depth() -> None:
    st = _stmts()
    assert [s.kind for s in st] == [
        "DROP TABLE",
        "CREATE TABLE",
        "INSERT",
        "SET",
        "SELECT",
        "SELECT",
        "SELECT",
        "SELECT",
        "SYSTEM FLUSH",
        "DROP TABLE",
    ]
    assert [s.ordinal for s in st] == list(range(1, 11))
    assert "a;b" in st[2].body  # the ';' inside the literal did not split
    assert st[-1].body == "DROP TABLE t"  # no trailing ';' at EOF


def test_spans_are_file_lines_and_carry_the_hint_line() -> None:
    st = _stmts()
    assert (st[0].start_line, st[0].end_line) == (4, 4)
    # the leading comment belongs to the statement it precedes
    assert (st[1].start_line, st[1].end_line) == (5, 6)
    assert st[1].comments == "-- the table under test"
    assert (st[5].start_line, st[5].end_line) == (11, 11)
    # header lines are never a statement's comment
    assert all("Tags:" not in s.comments for s in st)


def test_hint_grammar_matches_testhint() -> None:
    st = _stmts()
    assert st[5].hint.errors == ("NUMBER_OF_ARGUMENTS_DOESNT_MATCH", "BAD_ARGUMENTS")
    assert st[6].hint.errors == ("62",)
    assert st[7].hint.echo == "echoOn"
    assert st[7].hint.errors == ()
    assert st[5].signature() == "SELECT · expects NUMBER_OF_ARGUMENTS_DOESNT_MATCH, BAD_ARGUMENTS"
    # a leading comment's hint is ignored, as the client ignores it
    only_leading = lang_sql_split.split_statements("-- { serverError 36 }\nSELECT 1;")
    assert only_leading[0].hint.errors == ()
    assert lang_sql_split.parse_hint(["-- { retry 5 until }"]).retry == "retry 5 until"


def test_statement_kind_reads_one_or_two_keywords() -> None:
    assert lang_sql_split.statement_kind("WITH 1 AS x SELECT x") == "SELECT"
    assert (
        lang_sql_split.statement_kind("CREATE OR REPLACE TABLE IF NOT EXISTS t (x Int)")
        == "CREATE TABLE"
    )
    assert (
        lang_sql_split.statement_kind("CREATE MATERIALIZED VIEW mv TO t AS SELECT 1")
        == "CREATE MATERIALIZED"
    )
    assert lang_sql_split.statement_kind("SYSTEM STOP MERGES t") == "SYSTEM STOP"
    assert lang_sql_split.statement_kind("ALTER TABLE t ADD COLUMN y Int") == "ALTER TABLE"
    assert lang_sql_split.statement_kind("  ") == "EMPTY"


def test_facets_record_what_is_written_with_a_confidence() -> None:
    st = _stmts()
    by = {
        (f.kind, f.name): f
        for f in lang_sql_facets.file_facets(lang_sql_model.parse_header(_SQL.decode()), st)
    }
    assert by[(lang_sql_model.FACET_TAG, "zookeeper")].ordinal == 0
    assert by[(lang_sql_model.FACET_ENGINE, "ReplicatedMergeTree")].ordinal == 2
    assert (
        by[(lang_sql_facets.FACET_TYPE, "DateTime")].confidence == lang_sql_model.CONF_KEYWORD_BOUND
    )
    assert by[(lang_sql_model.FACET_SETTING, "dialect")].ordinal == 4
    assert by[(lang_sql_model.FACET_SETTING, "max_threads")].ordinal == 4
    assert by[(lang_sql_model.FACET_SETTING, "optimize_read_in_order")].ordinal == 5
    assert by[(lang_sql_model.FACET_FORMAT, "TSV")].ordinal == 5
    fn = by[(lang_sql_model.FACET_FUNCTION, "quantileExactWeightedInterpolated")]
    assert fn.confidence == lang_sql_model.CONF_CALL_SHAPED and fn.line == 10
    assert by[(lang_sql_model.FACET_ERROR, "NUMBER_OF_ARGUMENTS_DOESNT_MATCH")].ordinal == 6
    assert by[(lang_sql_model.FACET_SYSTEM_TABLE, "query_log")].ordinal == 8
    assert by[(lang_sql_model.FACET_STATEMENT, "SYSTEM FLUSH")].ordinal == 9
    # an object name followed by a column list is not a call; nor is a keyword
    assert (lang_sql_model.FACET_FUNCTION, "t") not in by
    assert (lang_sql_model.FACET_FUNCTION, "VALUES") not in by
    assert (lang_sql_model.FACET_FUNCTION, "count") in by
    # ClickHouse keywords that are also functions stay callable facets
    kw = lang_sql_split.split_statements(
        "SELECT range(3), if(1, 2, 3), array(1), position('ab', 'b');"
    )
    names = {
        f.name
        for f in lang_sql_facets.statement_facets(kw[0])
        if f.kind == lang_sql_model.FACET_FUNCTION
    }
    assert {"range", "if", "array", "position"} <= names


def test_parse_sql_yields_one_statement_symbol_per_record() -> None:
    pf = ls.parse_sql("tests/queries/0_stateless/00001_x.sql", _SQL)
    assert pf.lang == "sql" and pf.tree is None
    names = [s.name for s in pf.symbols if s.kind == "statement"]
    assert names == [f"stmt_{i:04d}" for i in range(1, 11)]
    assert [s.name for s in pf.symbols if s.kind != "statement"] == ["t"]
    s6 = pf.symbols[5]
    assert s6.qualified_name == "tests.queries.0_stateless.00001_x::stmt_0006"
    assert s6.kind == "statement"
    assert s6.signature == "SELECT · expects NUMBER_OF_ARGUMENTS_DOESNT_MATCH, BAD_ARGUMENTS"
    assert (s6.start_line, s6.end_line) == (11, 11)
    assert s6.id == symbol_id(s6.qualified_name, s6.signature)
    assert pf.symbols[1].doc_comment == "-- the table under test"


def test_ingest_dispatches_sql_to_the_statement_parser(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_sql_statement_records", True)
    pf = parse_file("tests/queries/0_stateless/00001_x.sql", _SQL)
    assert pf is not None and pf.lang == "sql"
    assert len([s for s in pf.symbols if s.kind == "statement"]) == 10
    j2 = parse_file("tests/queries/0_stateless/00002_x.sql.j2", b"SELECT {{ x }};\nSELECT 2;\n")
    assert j2 is not None and j2.lang == "sql" and len(j2.symbols) == 2
    # off: the line-window chunker and no symbols, as before
    monkeypatch.setattr(settings, "code_sql_statement_records", False)
    off = parse_file("tests/queries/0_stateless/00001_x.sql", _SQL)
    assert off is not None and off.lang == "sql" and off.symbols == []
    assert parse_file("tests/queries/0_stateless/00002_x.sql.j2", b"SELECT 1;") is None


def test_chunks_pack_whole_statements_and_never_cut_one() -> None:
    pf = ls.parse_sql("tests/queries/0_stateless/00001_x.sql", _SQL)
    u = ls.sql_units(pf, target_chars=200)
    assert u.chunks[0].kind == "file_header" and u.chunks[0].start_line == 1
    body = [c for c in u.chunks if c.kind != "file_header"]
    assert len(body) >= 3
    # every statement sits whole inside exactly one chunk span
    for st in u.statements:
        inside = [c for c in body if c.start_line <= st.start_line and st.end_line <= c.end_line]
        assert len(inside) == 1, st
    # chunks are contiguous, ordered, and named by their statements
    assert body[0].scope.startswith("stmt_0001")
    assert all(a.end_line < b.start_line for a, b in zip(body, body[1:], strict=False))
    assert all(c.symbol_ids for c in body)
    assert len(u.prefixes) == len(u.chunks)


def test_tiny_file_is_one_chunk_not_dropped() -> None:
    pf = ls.parse_sql("tests/queries/0_stateless/00008_array_join.sql", b"SELECT arrayJoin([1]);\n")
    u = ls.sql_units(pf)
    assert len(u.chunks) == 1 and u.chunks[0].scope == "stmt_0001"
    assert u.chunks[0].content == "SELECT arrayJoin([1]);\n"


def test_oversized_statement_splits_at_lines_with_a_marker() -> None:
    big = "SELECT\n" + "".join(f"  {i},\n" for i in range(600)) + "  1;\n"
    pf = ls.parse_sql("tests/queries/0_stateless/00003_x.sql", big.encode())
    u = ls.sql_units(pf, target_chars=2000, max_chars=1500)
    pieces = [c for c in u.chunks if c.kind == "other"]
    assert len(pieces) > 1
    assert all(len(c.content) <= 1500 for c in pieces)
    assert all("… continued at line" in c.content for c in pieces[:-1])
    assert all(c.scope == "stmt_0001" for c in pieces)
    assert pieces[0].start_line == 1 and pieces[-1].end_line == 602


def test_context_prefix_names_the_test_tags_kinds_and_facets() -> None:
    pf = ls.parse_sql("tests/queries/0_stateless/00001_x.sql", _SQL)
    u = ls.sql_units(pf)
    p = [x for x in u.prefixes if "stmt_0001" in x][0]
    assert p.startswith("00001_x.sql · tags: no-fasttest, zookeeper · stmt_0001–stmt_0010: ")
    assert "SELECT×4" in p
    assert "sets: dialect, max_threads, optimize_read_in_order" in p
    assert "engine: ReplicatedMergeTree" in p and "expects: NUMBER_OF_ARGUMENTS_DOESNT_MATCH" in p
    # the prefix's identifiers ride the lexemes ahead of the content
    chunk = [c for c in u.chunks if c.kind == "other"][0]
    assert chunk.extra_lexemes.startswith("00001") and "zookeeper" in chunk.extra_lexemes
    assert chunk.lexemes.index("zookeeper") < chunk.lexemes.index(
        "quantileexactweightedinterpolated"
    )


def test_is_sql_path() -> None:
    assert ls.is_sql_path("tests/queries/0_stateless/1.sql")
    assert ls.is_sql_path("tests/queries/0_stateless/1.sql.j2")
    assert not ls.is_sql_path("tests/queries/0_stateless/1.sh")
    assert not ls.is_sql_path("tests/queries/0_stateless/1.reference")
