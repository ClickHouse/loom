"""`await f<T>(x)`, `f!(x)` and `(f)(x)` are calls of f —
tree-sitter wraps the callee, the extractor unwraps it."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import call_sites, extract_edges_and_misses

TABLE = {
    "queryClickhouse": [("q1", "src.ch::queryClickhouse", "function")],
    "fetchThing": [("f1", "src.api::fetchThing", "function")],
}


def test_awaited_generic_and_non_null_callees_are_sites_and_bind() -> None:
    pf = parse_file(
        "src/a.ts",
        b"import { queryClickhouse } from './ch'\nimport { fetchThing } from './api'\n"
        b"export async function run() {\n"
        b"  const rows = await queryClickhouse<{ count: string }>({ query: 'x' })\n"
        b"  const t = await fetchThing!(1)\n"
        b"  const u = (fetchThing)(2)\n"
        b"  return [rows, t, u]\n}\n",
    )
    assert [n for _l, n in call_sites(pf)] == ["queryClickhouse", "fetchThing", "fetchThing"]
    edges, misses = extract_edges_and_misses(pf, TABLE)
    assert [
        (e.to_qualified_name, e.line, e.confidence) for e in edges if e.edge_kind == "calls"
    ] == [
        ("src.ch::queryClickhouse", 4, 1.0),
        ("src.api::fetchThing", 5, 1.0),
        ("src.api::fetchThing", 6, 1.0),
    ]
    assert misses == []


def test_an_awaited_member_call_keeps_its_receiver() -> None:
    from server.code.domain.extract.edges_treesitter import _receiver_text

    pf = parse_file(
        "src/b.ts", b"async function r(client: Ch) {\n  return await client.runQuery<Row>('q')\n}\n"
    )
    calls = []

    def walk(n):
        for c in n.named_children:
            if c.type == "call_expression":
                calls.append(c)
            walk(c)

    walk(pf.tree.root_node)
    assert [_receiver_text(c, pf.source) for c in calls] == ["client"]
    assert [n for _l, n in call_sites(pf)] == ["runQuery"]
