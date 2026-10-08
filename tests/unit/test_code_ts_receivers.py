"""A TypeScript / JavaScript member call binds through the
receiver's type when the tree states it — a parameter annotation, a typed
declaration, a construction, an assertion, a class field behind `this.x`
or `this` itself — narrowing rung 3's candidates to `…::T::method`."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import (
    TYPED_RECEIVER_CONFIDENCE,
    extract_edges_and_misses,
)
from server.code.domain.extract.ts_receivers import ReceiverTyper

_SRC = b"""import { ClickHouseClient } from './client';
export class Service {
  private client: ClickHouseClient;
  constructor(private readonly conn: Connection, opts: Opts) { this.client = new ClickHouseClient(opts); this.fmt = new Formatter(); }
  run(query: string, other: Reply | undefined): void {
    const local: Formatter = makeFormatter();
    const reply = new Reply(this);
    let x = createThing();
    const y = raw as Widget;
    this.client.query(query); other?.send(1); local.format(2); reply.code(200); x.go(); this.conn.exec(); this.fmt.f(); y.w();
  }
}
export function handler(client: ClickHouseClient, { a }: Opts) { client.insert(a); }
const shared = new Registry();
function top() { shared.register(); }
"""


def _table(*pairs: tuple[str, str]) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {}
    for i, (bare, qn) in enumerate(pairs):
        out.setdefault(bare, []).append((f"id{i}", qn, "method"))
    return out


def test_receiver_types_come_from_annotations_constructions_fields_and_this() -> None:
    pf = parse_file("src/a.ts", _SRC)
    t = ReceiverTyper(pf)
    assert t.type_of(10, "this.client") == "ClickHouseClient"  # field annotation
    assert t.type_of(10, "other") == "Reply"  # `Reply | undefined` parameter
    assert t.type_of(10, "local") == "Formatter"  # typed const
    assert t.type_of(10, "reply") == "Reply"  # construction
    assert t.type_of(10, "y") == "Widget"  # assertion
    assert t.type_of(10, "this.conn") == "Connection"  # constructor parameter property
    assert t.type_of(10, "this.fmt") == "Formatter"  # `this.fmt = new Formatter()`
    assert t.type_of(10, "this") == "Service"
    assert t.type_of(10, "x") == ""  # a call's result: the tree does not say
    assert t.type_of(13, "client") == "ClickHouseClient"  # a parameter of another function
    assert (
        t.type_of(15, "shared") == "Registry" and t.type_of(13, "shared") == "Registry"
    )  # module const


def test_member_calls_bind_through_the_receiver_type_at_0_9() -> None:
    """Two `query` methods in the repository: rung 3 declines, rung 2b binds
    the one on the receiver's type; an untyped receiver stays a miss."""
    pf = parse_file("src/a.ts", _SRC)
    table = _table(
        ("query", "src.client::ClickHouseClient::query"),
        ("query", "src.other::Other::query"),
        ("send", "src.reply::Reply::send"),
        ("send", "src.sock::Socket::send"),
        ("format", "src.fmt::Formatter::format"),
        ("format", "src.util::format"),
        ("code", "src.reply::Reply::code"),
        ("code", "src.http::Response::code"),
        ("go", "src.a::Thing::go"),
        ("go", "src.b::Other::go"),
        ("exec", "src.conn::Connection::exec"),
        ("exec", "src.shell::exec"),
        ("insert", "src.client::ClickHouseClient::insert"),
        ("insert", "src.db::Table::insert"),
        ("register", "src.reg::Registry::register"),
        ("register", "src.other::Plugin::register"),
    )
    edges, misses = extract_edges_and_misses(pf, table, None)
    bound = {
        (e.from_qualified_name.split("::", 1)[1], e.to_qualified_name.split("::", 1)[1]): round(
            e.confidence, 1
        )
        for e in edges
        if e.edge_kind == "calls"
    }
    assert bound[("Service::run", "ClickHouseClient::query")] == TYPED_RECEIVER_CONFIDENCE
    assert bound[("Service::run", "Reply::send")] == TYPED_RECEIVER_CONFIDENCE  # `other?.send`
    assert bound[("Service::run", "Formatter::format")] == TYPED_RECEIVER_CONFIDENCE
    assert bound[("Service::run", "Reply::code")] == TYPED_RECEIVER_CONFIDENCE
    assert bound[("Service::run", "Connection::exec")] == TYPED_RECEIVER_CONFIDENCE
    assert bound[("handler", "ClickHouseClient::insert")] == TYPED_RECEIVER_CONFIDENCE
    assert bound[("top", "Registry::register")] == TYPED_RECEIVER_CONFIDENCE
    assert ("Service::run", "Thing::go") not in bound  # `x = createThing()`: untyped
    assert any(m.callee_name == "go" and m.disposition == "ambiguous" for m in misses)


def test_commonjs_prototype_methods_are_units_and_targets() -> None:
    """`Reply.prototype.send = function` is `Reply::send`; a
    `new Reply()` receiver binds `reply.send()` to it (JavaScript has no
    annotations — constructions are the type facts)."""
    lib = parse_file(
        "lib/reply.js",
        b"""'use strict'
function Reply (res) { this.raw = res }
Reply.prototype.send = function (payload) { return this.raw.end(payload) }
Reply.prototype.code = function (c) { this.statusCode = c; return this }
module.exports = Reply
""",
    )
    kinds = {s.qualified_name.split("::", 1)[1]: s.kind for s in lib.symbols}
    assert kinds["Reply::send"] == "function" and kinds["Reply::code"] == "function"
    assert kinds["Reply"] == "function" and kinds["default"] == "const"
    user = parse_file(
        "lib/route.js",
        b"""const Reply = require('./reply')
function handle (res) {
  const reply = new Reply(res)
  reply.code(200)
  reply.send('ok')
}
""",
    )
    table = _table(
        ("send", "lib.reply::Reply::send"),
        ("send", "lib.other::Socket::send"),
        ("code", "lib.reply::Reply::code"),
        ("code", "lib.http::Response::code"),
    )
    edges, _ = extract_edges_and_misses(user, table, None)
    got = {e.to_qualified_name for e in edges if e.edge_kind == "calls"}
    assert got == {"lib.reply::Reply::send", "lib.reply::Reply::code"}
