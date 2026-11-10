"""SQL inside TypeScript template literals — islands found
by shape, dialect from the client, statement records under the enclosing
function, facets, edges to the tables the migrations create."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("tree_sitter_typescript")

from server.code.app.index.ingest_chunks import _chunks_for
from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.lang_sql import parse_sql
from server.code.domain.extract.lang_ts_islands import (
    find_islands,
    island_edges,
    island_records,
    template_text,
    ts_units,
)
from server.code.domain.extract.lang_typescript import parse_typescript
from server.code.domain.extract.name_scope import SQL_LANG, gate_name_table
from tests.code_lanes.corpus import lane_settings_on

_REPO = b"""import { queryClickhouse, commandClickhouse } from "./clickhouse";
import { prisma } from "../db";

export const getScoreById = async (projectId: string, id: string) => {
  const query = `
    SELECT *
    FROM scores s FINAL
    WHERE s.project_id = {projectId: String}
      AND s.id = {id: String}
      ${extra}
    ORDER BY s.event_ts DESC
    LIMIT 1 BY s.id
  `;
  const rows = await queryClickhouse<Row>({ query, params: { projectId, id } });
  return rows.shift();
};

export async function deleteScores(projectId: string) {
  await commandClickhouse({
    query: `DELETE FROM scores WHERE project_id = {projectId: String} SETTINGS mutations_sync = 1`,
    params: { projectId },
  });
  await prisma.$executeRaw`UPDATE "users" SET "name" = ${name} WHERE id = ${id}`;
  return `not sql: ${projectId}`;
}

const label = `Hello ${world}`;
"""

_MIGRATION = b"""CREATE TABLE scores {CLICKHOUSE_CLUSTER_CLAUSE} (
    `id` String,
    `project_id` String,
    event_ts DateTime64(3)
) ENGINE = {CLICKHOUSE_REPLICATION_PREFIX}ReplacingMergeTree(event_ts) ORDER BY (project_id, id);
"""


@pytest.fixture
def islands_on() -> Iterator[None]:
    """The corpus's lane settings (statement records and TypeScript islands
    among them) for the test, then as they were."""
    with lane_settings_on():
        yield


def test_islands_are_found_by_shape_and_keep_their_lines() -> None:
    pf = parse_typescript("packages/shared/src/server/repositories/scores.ts", _REPO)
    islands = find_islands(pf)
    assert [(i.line, i.dialect, i.interpolated) for i in islands] == [
        (5, "clickhouse", True),
        (20, "clickhouse", False),
        (23, "postgres", True),
    ]
    assert "__var__" in islands[0].text and "{projectId: String}" in islands[0].text
    statements, scopes, _ = island_records(pf)
    mod = "packages.shared.src.server.repositories.scores"
    assert [(s.ordinal, s.kind, s.start_line) for s in statements] == [
        (1, "SELECT", 6),
        (2, "DELETE", 20),
        (3, "UPDATE", 23),
    ]
    assert scopes == [f"{mod}::getScoreById", f"{mod}::deleteScores", f"{mod}::deleteScores"]


def test_prose_literals_that_open_with_a_verb_are_not_islands() -> None:
    from server.code.domain.extract.lang_ts_islands import looks_like_sql

    for prose in (
        "Delete ${n} items?",
        "Show ${n} more",
        "Select all ${n} items across ${p} pages",
        "Update available",
        'Copy "${x}"',
        "Insert the original text, unescaped",
    ):
        assert not looks_like_sql(prose.replace("${n}", "__var__")), prose
    assert looks_like_sql("SELECT * FROM t")  # upper verb, one keyword
    assert looks_like_sql("select __var__ from scores s WHERE s.project_id = 1")  # two keywords
    assert not looks_like_sql("select all")  # a lower-case verb alone
    assert looks_like_sql("select all", inside_client=True)  # the client runs what it is handed
    assert looks_like_sql("__var__ SETTINGS x = 1", inside_client=True)
    src = b"""export const Btn = () => <button title={`Delete ${n} items?`}>{`Show ${n} more`}</button>;
export const q = `select id from scores s WHERE s.project_id = {p: String}`;
"""
    pf = parse_typescript("web/src/x.tsx", src)
    assert [i.line for i in find_islands(pf)] == [2]


def test_template_text_keeps_newlines_of_a_multiline_substitution() -> None:
    src = b"const q = `SELECT ${\n  a\n} FROM t`;\n"
    pf = parse_typescript("a.ts", src)
    node = next(
        c
        for c in pf.tree.root_node.named_children[0].named_children[0].named_children
        if c.type == "template_string"
    )
    text, interpolated = template_text(node, src)
    assert interpolated and text == "SELECT __var__\n\n FROM t"


def test_units_carry_symbols_facets_and_prefixes() -> None:
    pf = parse_typescript("packages/shared/src/server/repositories/scores.ts", _REPO)
    chunks = chunk_file(pf)
    tu = ts_units(pf, chunks)
    names = [s.qualified_name.split("::", 1)[1] for s in tu.symbols]
    assert names == [
        "getScoreById::stmt_0001",
        "deleteScores::stmt_0002",
        "deleteScores::stmt_0003",
    ]
    kinds = {(f.kind, f.name) for f in tu.facets}
    assert ("dialect", "clickhouse") in kinds and ("dialect", "postgres") in kinds
    assert ("table_read", "scores") in kinds and ("table_write", "scores") in kinds
    assert ("table_write", "users") in kinds and ("setting", "mutations_sync") in kinds
    assert ("table_read", "LIMIT") not in kinds  # `DESC LIMIT` is not a DESCRIBE
    assert not any("__var__" in f.name for f in tu.facets)
    sel = next(f for f in tu.facets if f.kind == "table_read" and f.name == "scores")
    assert sel.confidence == 0.7  # interpolated statement: floored
    fn = next(c for c in tu.chunks if c.scope.endswith("::getScoreById"))
    assert tu.symbols[0].id in fn.symbol_ids
    prefix = tu.prefixes[tu.chunks.index(fn)]
    assert (
        prefix.startswith("scores.ts")
        and "reads: scores" in prefix
        and "stmt_0001: SELECT" in prefix
    )


def test_edges_bind_to_the_migrations_table_from_statement_and_function(
    islands_on: None,
) -> None:
    mig = parse_sql(
        "packages/shared/clickhouse/migrations/canonical/0003_scores.up.sql", _MIGRATION
    )
    table = next(s for s in mig.symbols if s.kind == "table")
    assert table.name == "scores"
    pf = parse_typescript("packages/shared/src/server/repositories/scores.ts", _REPO)
    statements, scopes, _ = island_records(pf)
    name_table = gate_name_table({"scores": [(table.id, table.qualified_name, "table")]}, SQL_LANG)
    edges = island_edges(pf, statements, scopes, name_table, rw_kinds=True)
    shapes = sorted((e.from_qualified_name.split("::")[-1], e.edge_kind, e.line) for e in edges)
    assert shapes == [
        ("deleteScores", "writes", 20),
        ("getScoreById", "reads", 7),
        ("stmt_0001", "reads", 7),
        ("stmt_0002", "writes", 20),
    ]
    assert all(e.to_symbol_id == table.id for e in edges)


def test_chunks_for_adds_island_symbols_to_the_parse(islands_on: None) -> None:
    path = "packages/shared/src/server/repositories/scores.ts"
    pf = parse_file(path, _REPO)
    assert pf is not None and not any(s.kind == "statement" for s in pf.symbols)
    chunks, prefixes, facets = _chunks_for(pf, path, _REPO, 6000)
    assert sum(1 for s in pf.symbols if s.kind == "statement") == 3
    assert prefixes and facets


def test_a_table_defined_in_two_stores_binds_by_the_islands_dialect(
    islands_on: None,
) -> None:
    from server.code.domain.extract.lang_ts_islands import narrow_by_dialect

    ch = (
        "id-ch",
        "packages.shared.clickhouse.migrations.canonical.0003_scores.up::scores",
        "table",
    )
    pg = ("id-pg", "packages.shared.prisma.migrations.20230523_rename.migration::scores", "table")
    table = {
        "scores": [ch, pg],
        "events_core": [("id-ec", "m.0040_create_events_core.up::events_core", "table")],
    }
    dialects = {
        "packages.shared.clickhouse.migrations.canonical.0003_scores.up": "clickhouse",
        "packages.shared.prisma.migrations.20230523_rename.migration": "postgres",
    }
    assert narrow_by_dialect(table, "clickhouse", dialects)["scores"] == [ch]
    assert narrow_by_dialect(table, "postgres", dialects)["scores"] == [pg]
    # a dialect nobody's file speaks, or no dialect at all: the table is untouched
    assert narrow_by_dialect(table, "mysql", dialects)["scores"] == [ch, pg]
    assert narrow_by_dialect(table, "", dialects) is table
    assert narrow_by_dialect(table, "clickhouse", None) is table
    # an unambiguous name is never narrowed
    assert narrow_by_dialect(table, "postgres", dialects)["events_core"] == table["events_core"]
    # end to end: the ClickHouse island in _REPO binds the ClickHouse table only
    pf = parse_typescript("packages/shared/src/server/repositories/scores.ts", _REPO)
    statements, scopes, _ = island_records(pf)
    edges = island_edges(
        pf,
        statements,
        scopes,
        gate_name_table(table, SQL_LANG),
        rw_kinds=True,
        scope_dialects=dialects,
    )
    by_stmt = {e.from_qualified_name.split("::")[-1]: e.to_qualified_name for e in edges}
    assert by_stmt["stmt_0001"] == ch[1] and by_stmt["stmt_0002"] == ch[1]
    assert "stmt_0003" not in by_stmt  # the Postgres UPDATE names `users`, which nobody defines
    # without the dialects the shared name is ambiguous and binds nothing
    edges2 = island_edges(pf, statements, scopes, gate_name_table(table, SQL_LANG), rw_kinds=True)
    assert not any(e.to_qualified_name.endswith("::scores") for e in edges2)


def test_a_long_callee_is_read_at_its_ends() -> None:
    """A callee longer than 128 bytes gives its client cue from its first and last 64
    bytes: a name at either end still reads, one in the middle does not."""
    middle = ".a" * 100
    src = (
        f"x{middle}.queryClickhouse(`SELECT a FROM t`);\n"
        f"queryClickhouse{middle}(`SELECT a FROM t`);\n"
        f"x{middle}.queryClickhouse{middle}(`SELECT a FROM t`);\n"
    ).encode()
    islands = find_islands(parse_typescript("src/a.ts", src))
    assert [i.dialect for i in islands] == ["clickhouse", "clickhouse", "sql"]
