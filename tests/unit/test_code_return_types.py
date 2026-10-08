"""A function symbol carries the type it returns and
a binding initialised by a call takes that type once the callee is known;
nested test blocks are units of their own."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import (
    TYPED_RECEIVER_CONFIDENCE,
    callee_targets,
    extract_edges_and_misses,
)
from server.code.domain.extract.ts_receivers import ReceiverTyper
from server.code.store.index import return_type_from_signature


def test_a_function_symbol_carries_the_type_it_returns() -> None:
    src = b"""export function createClient(o: Opts): ClickHouseClient { return new ClickHouseClient(o); }
export async function load(): Promise<Reply> { return x; }
export function make(res) { return new Reply(res); }
/** @returns {Formatter} the formatter */
function mk(a) { return a ? new Formatter() : other(); }
export const arrow = (): Widget => w;
function untyped(a) { if (a) return new A(); return new B(); }
export function prim(): string { return ""; }
"""
    pf = parse_file("src/a.ts", src)
    got = {s.name: s.return_type for s in pf.symbols}
    assert got == {
        "createClient": "ClickHouseClient",
        "load": "Reply",
        "make": "Reply",
        "mk": "Formatter",
        "arrow": "Widget",
        "untyped": "",
        "prim": "",
    }
    assert return_type_from_signature("(o: Opts): ClickHouseClient") == "ClickHouseClient"
    assert return_type_from_signature("(): Promise<Reply>") == "Reply"
    assert (
        return_type_from_signature("(a: string): void") == ""
        and return_type_from_signature("(a)") == ""
    )


def test_a_call_typed_binding_resolves_through_the_callees_return_type() -> None:
    src = b"""import { createClient } from './client';
function local(): Reply { return new Reply(); }
export async function run(opts: Opts) {
  const client = createClient(opts);
  const reply = local();
  const other = await fetchThing();
  client.query('x'); reply.send(1); other.go();
}
export class Svc {
  constructor(opts: Opts) { this.client = createClient(opts); }
  go() { this.client.insert(1); }
}
"""
    pf = parse_file("packages/node/src/a.ts", src)
    typer = ReceiverTyper(pf)
    assert typer.callees() == {"createClient", "local", "fetchThing"}
    assert typer.type_of(7, "client") == ""  # unknown until the callee's return is bound
    typer.bind_returns({"createClient": "ClickHouseClient", "local": "Reply"})
    assert typer.type_of(7, "client") == "ClickHouseClient"
    assert typer.type_of(7, "reply") == "Reply"
    assert typer.type_of(7, "other") == ""  # fetchThing unknown
    assert typer.type_of(11, "this.client") == "ClickHouseClient"
    table = {
        "createClient": [("c1", "packages.node.src.client::createClient", "function")],
        "query": [
            ("q1", "packages.common::ClickHouseClient::query", "function"),
            ("q2", "packages.other::Other::query", "function"),
        ],
        "send": [
            ("s1", "src.reply::Reply::send", "function"),
            ("s2", "src.sock::Socket::send", "function"),
        ],
        "insert": [
            ("i1", "packages.common::ClickHouseClient::insert", "function"),
            ("i2", "packages.db::Table::insert", "function"),
        ],
    }
    pf.includes = ["packages/node/src/client.ts"]
    # what ingest reads before the ladder: the callee -> qualified name map
    assert callee_targets(pf, table) == {"createClient": "packages.node.src.client::createClient"}
    edges, _ = extract_edges_and_misses(pf, table, None, {"createClient": "ClickHouseClient"})
    got = {
        (e.from_qualified_name.split("::", 1)[1], e.to_qualified_name): round(e.confidence, 1)
        for e in edges
        if e.edge_kind == "calls"
        and e.to_qualified_name.split("::")[-1] in ("query", "send", "insert")
    }
    assert got == {
        ("run", "packages.common::ClickHouseClient::query"): TYPED_RECEIVER_CONFIDENCE,
        ("run", "src.reply::Reply::send"): TYPED_RECEIVER_CONFIDENCE,  # same-file callee, no read
        ("Svc::go", "packages.common::ClickHouseClient::insert"): TYPED_RECEIVER_CONFIDENCE,
    }


def test_jsdoc_types_bind_javascript_receivers() -> None:
    src = b"""/**
 * @param {Reply} reply the reply
 * @param {string} name
 */
function handle (reply, name) { reply.send(name) }
/** @type {Formatter} */
const fmt = build()
function run () { fmt.format(1) }
"""
    pf = parse_file("lib/a.js", src)
    typer = ReceiverTyper(pf)
    assert typer.type_of(5, "reply") == "Reply" and typer.type_of(5, "name") == ""
    assert typer.type_of(8, "fmt") == "Formatter"


def test_nested_test_blocks_are_units_under_their_parent() -> None:
    src = b"""const { test } = require('node:test')
test('outer block', async t => {
  await t.test('inner one', async t => { run(1) })
  t.test('inner two', () => { run(2) })
  run(3)
})
"""
    pf = parse_file("test/x.test.js", src)
    units = {s.qualified_name.split("::", 1)[1]: (s.start_line, s.end_line) for s in pf.symbols}
    assert units == {
        "test_outer_block": (2, 6),
        "test_outer_block::test_inner_one": (3, 3),
        "test_outer_block::test_inner_two": (4, 4),
    }
    table = {"run": [("r1", "lib.run::run", "function")]}
    edges, _ = extract_edges_and_misses(pf, table, None)
    callers = sorted(
        e.from_qualified_name.split("::", 1)[1] for e in edges if e.edge_kind == "calls"
    )
    assert callers == [
        "test_outer_block",
        "test_outer_block::test_inner_one",
        "test_outer_block::test_inner_two",
    ]
