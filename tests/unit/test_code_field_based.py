"""The script call ladder binds through the file's import
first (scope graphs: resolution follows the import edge) and splits rung 3's
candidates the way the language does (a property access names a field,
whatever the receiver)."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import (
    BARREL_SCOPED_CONFIDENCE,
    IMPORT_BOUND_CONFIDENCE,
    REPO_WIDE_CONFIDENCE,
    extract_edges_and_misses,
)


def _table(*rows: tuple[str, str, str]) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {}
    for i, (bare, qn, kind) in enumerate(rows):
        out.setdefault(bare, []).append((f"id{i}", qn, kind))
    return out


def test_a_bare_call_binds_through_its_import_and_a_library_import_binds_nothing() -> None:
    src = b"""import { createClient } from './client';
import { parseArgs } from 'node:util';
import * as fmt from '../utils/format';
export function main() {
  createClient({});
  parseArgs(process.argv);
  fmt.formatQueryParams({});
}
"""
    pf = parse_file("packages/client-node/src/main.ts", src)
    # what ingest's include resolution would have found: two repo modules, no node:util
    pf.includes = ["packages/client-node/src/client.ts", "packages/client-node/utils/format.ts"]
    table = _table(
        ("createClient", "packages.client-node.src.client::createClient", "function"),
        ("createClient", "packages.client-web.src.client::createClient", "function"),
        (
            "parseArgs",
            "tests.runner.src.args::parseArgs",
            "function",
        ),  # a repo namesake of node:util's
        ("formatQueryParams", "packages.client-node.utils.format::formatQueryParams", "function"),
        ("formatQueryParams", "packages.client-web.utils.format::formatQueryParams", "function"),
    )
    edges, misses = extract_edges_and_misses(pf, table, None)
    got = {e.to_qualified_name: round(e.confidence, 2) for e in edges if e.edge_kind == "calls"}
    assert got == {
        "packages.client-node.src.client::createClient": IMPORT_BOUND_CONFIDENCE,
        "packages.client-node.utils.format::formatQueryParams": IMPORT_BOUND_CONFIDENCE,
    }
    assert not any(m.callee_name == "parseArgs" for m in misses) or all(
        m.disposition != "ambiguous" for m in misses if m.callee_name == "parseArgs"
    )


def test_an_import_through_a_barrel_narrows_to_the_barrels_subtree() -> None:
    src = b"""import { createClient } from '../src';
export function run() { return createClient({}); }
"""
    pf = parse_file("packages/client-node/__tests__/run.test.ts", src)
    pf.includes = ["packages/client-node/src/index.ts"]  # the barrel the import resolved to
    table = _table(
        ("createClient", "packages.client-node.src.client::createClient", "function"),
        ("createClient", "packages.client-web.src.client::createClient", "function"),
    )
    edges, _ = extract_edges_and_misses(pf, table, None)
    got = {e.to_qualified_name: round(e.confidence, 2) for e in edges if e.edge_kind == "calls"}
    assert got == {"packages.client-node.src.client::createClient": BARREL_SCOPED_CONFIDENCE}


def test_rung_three_is_field_based_for_script_calls() -> None:
    """`x.insert()` with an untyped `x` prefers member definitions, a bare
    `insert()` free ones — each unique on its side; a member-form call whose
    name no class defines (`lib.decorated()`, the CommonJS idiom) is a call
    to the free function."""
    src = b"""export function a(x: any) { x.insert(1); lib.decorated(); }
export function b() { insert(2); }
"""
    pf = parse_file("src/a.ts", src)
    table = _table(
        ("insert", "src.db.Table::Table::insert", "function"),
        ("insert", "src.util.insert::insert", "function"),
        ("decorated", "lib.plugin::decorated", "function"),
    )
    edges, misses = extract_edges_and_misses(pf, table, None)
    got = {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name): round(e.confidence, 2)
        for e in edges
        if e.edge_kind == "calls"
    }
    assert got == {
        ("a", "src.db.Table::Table::insert"): REPO_WIDE_CONFIDENCE,
        ("a", "lib.plugin::decorated"): REPO_WIDE_CONFIDENCE,
        ("b", "src.util.insert::insert"): REPO_WIDE_CONFIDENCE,
    }
    assert not misses


def test_a_workspace_package_import_is_not_a_library() -> None:
    """`@clickhouse/client-common` resolves through the
    package directory (its exports table), a path the extension
    ladder never probes; the name is the repository's, and a relative import
    that resolved to nothing is a miss, never a library."""
    from server.code.domain.extract.lang_typescript_imports import external_names

    src = b"""import { encodeJSON } from '@clickhouse/client-common';
import { readFileSync } from 'node:fs';
import { helper } from './missing';
import { z } from 'zod';
export function f() { encodeJSON(1); readFileSync('x'); helper(); z.object({}); }
"""
    pf = parse_file("packages/client-node/src/encoder.ts", src)
    pf.includes = ["packages/client-common/src/data_formatter/formatter.ts"]
    assert external_names(pf, set(pf.includes)) == {"readFileSync", "z"}
