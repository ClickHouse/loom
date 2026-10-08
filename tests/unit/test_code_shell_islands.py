"""A shell test's SQL islands become statement records and
its leftover chunks coalesce.

Islands: `-q "…"`, `--query "…"`, `--query="…"`, `-n "…"`, a heredoc body,
an `echo "…" | $CLICKHOUSE_CLIENT` pipe; expansions become placeholders and
lower the facet confidence; ordinals run over the file; records anchor at
the shell line. Coalescing: leftovers join across blank or unowned lines up
to a target and never across a function chunk.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_bash")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_shell_islands as li
from server.code.domain.extract.chunker import chunk_file

_SH = b"""#!/usr/bin/env bash
# Tags: zookeeper, no-parallel

CURDIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CURDIR"/../shell_config.sh

$CLICKHOUSE_CLIENT -q "DROP TABLE IF EXISTS t_$CLICKHOUSE_DATABASE"
${CLICKHOUSE_CLIENT} --query "CREATE TABLE t (d DateTime) ENGINE = ReplicatedMergeTree('/t/{shard}', '{replica}') ORDER BY d"

$CLICKHOUSE_CLIENT -n -q "
SET dialect = 'clickhouse';
SELECT quantileExactWeightedInterpolated(0.2)(d, 1) FROM t; -- { serverError BAD_ARGUMENTS }
"

echo "SELECT count() FROM system.query_log" | $CLICKHOUSE_CLIENT

$CLICKHOUSE_LOCAL --multiquery <<EOF
SELECT 1 FORMAT TSV;
SELECT arrayJoin([1, 2]);
EOF

function check() {
    $CLICKHOUSE_CLIENT -q "SYSTEM SYNC REPLICA t"
}

check
$CLICKHOUSE_CLIENT --query="DROP TABLE t"
"""


def _units() -> li.ShellUnits:
    pf = parse_file("tests/queries/0_stateless/00001_x.sh", _SH)
    assert pf is not None and pf.lang == "shell"
    return li.shell_units(pf, chunk_file(pf))


def test_islands_are_found_by_every_shape_in_source_order() -> None:
    u = _units()
    vias = [(i.via, i.line) for i in u.islands]
    assert vias == [
        ("inline", 8),
        ("inline", 9),
        ("inline", 11),
        ("pipe", 16),
        ("heredoc", 19),
        ("inline", 24),
        ("inline", 28),
    ]
    assert (
        u.islands[0].interpolated is True and u.islands[0].text == "DROP TABLE IF EXISTS t___var__"
    )
    assert u.islands[1].interpolated is False
    assert u.islands[6].text == "DROP TABLE t"  # --query="…" form
    assert "SELECT count() FROM system.query_log" == u.islands[3].text


def test_statements_anchor_at_the_shell_line_with_file_ordinals() -> None:
    u = _units()
    kinds = [(s.ordinal, s.start_line, s.kind) for s in u.statements]
    assert kinds == [
        (1, 8, "DROP TABLE"),
        (2, 9, "CREATE TABLE"),
        (3, 12, "SET"),
        (4, 13, "SELECT"),
        (5, 16, "SELECT"),
        (6, 19, "SELECT"),
        (7, 20, "SELECT"),
        (8, 24, "SYSTEM SYNC"),
        (9, 28, "DROP TABLE"),
    ]
    assert u.statements[3].hint.errors == ("BAD_ARGUMENTS",)
    assert u.symbols[3].qualified_name == "tests.queries.0_stateless.00001_x::stmt_0004"
    assert u.symbols[3].kind == "statement" and u.symbols[3].start_line == 13


def test_facets_carry_tags_and_interpolation_lowers_confidence() -> None:
    u = _units()
    by = {(f.kind, f.name): f for f in u.facets}
    assert by[("tag", "zookeeper")].ordinal == 0 and by[("tag", "no-parallel")].ordinal == 0
    assert by[("engine", "ReplicatedMergeTree")].confidence == 1.0
    assert by[("setting", "dialect")].confidence == 1.0
    assert by[("error_code", "BAD_ARGUMENTS")].ordinal == 4
    assert by[("system_table", "query_log")].ordinal == 5
    assert by[("format", "TSV")].ordinal == 6
    assert by[("function", "arrayJoin")].ordinal == 7
    # the interpolated island's names are capped at the interpolated confidence
    first = [f for f in u.facets if f.ordinal == 1 and f.kind != li.FACET_DIALECT]
    assert first and all(f.confidence == li.CONF_INTERPOLATED for f in first)
    assert ("dialect", "clickhouse") in by  # The client names the dialect
    assert by[("statement_kind", "DROP TABLE")].confidence == 1.0  # ordinal 9, no expansion
    assert not any(li.PLACEHOLDER in f.name for f in u.facets)


def test_leftovers_coalesce_but_never_across_a_function() -> None:
    pf = parse_file("tests/queries/0_stateless/00001_x.sh", _SH)
    raw = chunk_file(pf)
    u = li.shell_units(pf, raw, target_chars=2000)
    kinds = [c.kind for c in u.chunks]
    assert len(u.chunks) < len(raw)
    fn = [c for c in u.chunks if c.kind == "function"]
    assert len(fn) == 1 and fn[0].start_line == 23
    others = [c for c in u.chunks if c.kind == "other"]
    # everything above the function is one chunk, everything below another
    assert [(c.start_line, c.end_line) for c in others] == [(4, 21), (27, 28)]
    assert kinds.count("file_header") == 1
    # the coalesced chunk carries the island symbols and the prefix in its lexemes
    top = others[0]
    assert len(top.symbol_ids) == 7
    assert "zookeeper" in top.extra_lexemes and "quantileexactweightedinterpolated" in top.lexemes
    assert any("stmt_0001–stmt_0007" in p for p in u.prefixes.values())


def test_target_bounds_the_merge() -> None:
    pf = parse_file("tests/queries/0_stateless/00001_x.sh", _SH)
    raw = chunk_file(pf)
    small = li.shell_units(pf, raw, target_chars=200)
    big = li.shell_units(pf, raw, target_chars=100_000)
    assert len(small.chunks) > len(big.chunks)
    assert all(len(c.content) <= 200 or c.kind != "other" or c in raw for c in small.chunks)


def test_no_islands_means_no_records_and_the_same_chunks() -> None:
    src = b"#!/usr/bin/env bash\nset -e\n\nls -la\n\necho done\n"
    pf = parse_file("ci/x.sh", src)
    raw = chunk_file(pf)
    u = li.shell_units(pf, raw)
    assert u.islands == [] and u.statements == []
    assert [f for f in u.facets if f.kind != "program"] == []  # program facets aside
    assert [c.kind for c in u.chunks].count("file_header") == [c.kind for c in raw].count(
        "file_header"
    )


def test_client_flags_are_setting_facets_and_curl_bodies_are_islands() -> None:
    src = b"""#!/usr/bin/env bash
# Tags: no-fasttest
$CLICKHOUSE_CLIENT --max_threads=1 --async_insert 1 --query "INSERT INTO t VALUES (1)"
${CLICKHOUSE_CURL} -sS "$CLICKHOUSE_URL" -d "KILL QUERY WHERE query_id = 'x' SYNC"
${CLICKHOUSE_CURL} -sS "${CLICKHOUSE_URL}&async_insert=1&wait_for_async_insert=0&query=SELECT+1" --data-binary @- < data.tsv
$CLICKHOUSE_CLIENT --optimize_read_in_order=0 < "$CURDIR"/q.sql
"""
    pf = parse_file("tests/queries/0_stateless/00002_x.sh", src)
    u = li.shell_units(pf, chunk_file(pf))
    assert [(i.via, i.line) for i in u.islands] == [
        ("inline", 3),
        ("inline", 4),
        ("flags", 5),
        ("flags", 6),
    ]
    # URL parameters of an HTTP call are settings; `query=` is the query
    assert u.islands[2].settings == ("async_insert", "wait_for_async_insert")
    assert u.islands[0].settings == ("max_threads", "async_insert")
    assert u.islands[3].settings == ("optimize_read_in_order",) and u.islands[3].text == ""
    assert [s.kind for s in u.statements] == ["INSERT", "KILL QUERY"]
    by = {(f.kind, f.name): f for f in u.facets}
    assert by[("setting", "max_threads")].ordinal == 1 and by[("setting", "max_threads")].line == 3
    assert by[("setting", "async_insert")].confidence == 1.0
    # a flags-only island's settings are file-level facts (ordinal 0)
    assert by[("setting", "optimize_read_in_order")].ordinal == 0
    assert ("statement_kind", "KILL QUERY") in by
    # `--data-binary @-` is data, not a query; the client's own options are not settings
    assert ("setting", "query") not in by and ("setting", "sS") not in by
