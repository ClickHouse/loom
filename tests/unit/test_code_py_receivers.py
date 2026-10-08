"""A Python member call binds through the receiver's type
when the tree states it — a parameter annotation, an annotated or
constructed assignment, a class field behind `self.x`, `self` itself, or a
call whose return annotation is known — narrowing rung 3's candidates to
`…::T::method`."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import (
    TYPED_RECEIVER_CONFIDENCE,
    extract_edges_and_misses,
)
from server.code.domain.extract.py_receivers import PyReceiverTyper

_SRC = b"""from pkg.client import Client
from typing import Optional

def make_client(opts) -> Client:
    return Client(opts)

class Service:
    fmt: Formatter
    def __init__(self, conn: Connection, opts):
        self.client = Client(opts)
        self.conn = conn
        self.cache = build_cache()
    def run(self, other: Optional[Reply], third: "Widget", fourth: Reply | None, items: list[Row]):
        local: Formatter = get_formatter()
        made = Reply(self)
        x = make_client(1)
        self.client.query(1); other.send(1); third.w(); fourth.code(2); items.append(1)
        local.format(2); made.code(200); x.query(3); self.conn.exec(); self.cache.get(1)
        self.helper()

def handler(client: Client, plain):
    client.insert(1); plain.insert(2)

shared = Registry()
def top():
    shared.register()
"""


def _table(*pairs: tuple[str, str]) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {}
    for i, (bare, qn) in enumerate(pairs):
        out.setdefault(bare, []).append((f"id{i}", qn, "function"))
    return out


def test_receiver_types_from_annotations_constructions_fields_and_self() -> None:
    pf = parse_file("app/service.py", _SRC)
    t = PyReceiverTyper(pf)
    t.bind_returns(t.same_file_returns())
    assert t.type_of(17, "self.client") == "Client"  # `self.client = Client(opts)` in __init__
    assert t.type_of(17, "self.conn") == "Connection"  # a parameter stored on self
    assert t.type_of(17, "self.fmt") == "Formatter"  # annotated class field
    assert t.type_of(17, "self") == "Service"
    assert t.type_of(17, "other") == "Reply"  # Optional[Reply]
    assert t.type_of(17, "third") == "Widget"  # a string annotation
    assert t.type_of(17, "fourth") == "Reply"  # Reply | None
    assert t.type_of(17, "items") == "", "a list's receiver is the list, not its element"
    assert t.type_of(17, "local") == "Formatter"  # annotated assignment beats the call
    assert t.type_of(17, "made") == "Reply"  # construction
    assert t.type_of(17, "x") == "Client", "make_client() -> Client in this file"
    assert t.type_of(17, "self.cache") == "", "build_cache() returns nothing the file states"
    assert t.type_of(22, "client") == "Client"  # another function's parameter
    assert t.type_of(22, "plain") == ""
    assert t.type_of(26, "shared") == "Registry"  # module-level construction
    assert t.callees() == {"build_cache", "make_client"}, (
        "an annotated assignment waits on no callee"
    )
    assert (
        pf.symbols[0].qualified_name.endswith("::make_client")
        and pf.symbols[0].return_type == "Client"
    )


def test_typed_receivers_narrow_the_candidates_and_self_calls_stay_on_the_class() -> None:
    pf = parse_file("app/service.py", _SRC)
    table = _table(
        ("query", "pkg.client::Client::query"),
        ("query", "pkg.other::Other::query"),
        ("send", "pkg.reply::Reply::send"),
        ("send", "pkg.mail::Mailer::send"),
        ("exec", "pkg.conn::Connection::exec"),
        ("exec", "pkg.shell::Shell::exec"),
        ("insert", "pkg.client::Client::insert"),
        ("insert", "pkg.table::Table::insert"),
        ("register", "pkg.reg::Registry::register"),
        ("register", "pkg.hook::Hooks::register"),
        ("helper", "app.service::Service::helper"),
        ("get", "pkg.cache::Cache::get"),
        ("get", "pkg.map::Map::get"),
    )
    # the class's own method is in the table under the module scope the parser writes
    table["helper"] = [("idh", "app.service::Service::helper", "function")]
    edges, misses = extract_edges_and_misses(pf, table, None)
    by = {(e.to_qualified_name, e.line): e for e in edges}
    assert by[("pkg.client::Client::query", 17)].confidence == TYPED_RECEIVER_CONFIDENCE
    assert by[("pkg.reply::Reply::send", 17)].confidence == TYPED_RECEIVER_CONFIDENCE
    assert by[("pkg.client::Client::query", 18)].confidence == TYPED_RECEIVER_CONFIDENCE, (
        "x = make_client()"
    )
    assert by[("pkg.conn::Connection::exec", 18)].confidence == TYPED_RECEIVER_CONFIDENCE
    assert by[("pkg.client::Client::insert", 22)].confidence == TYPED_RECEIVER_CONFIDENCE
    assert by[("pkg.reg::Registry::register", 26)].confidence == TYPED_RECEIVER_CONFIDENCE
    # `self.cache.get(1)`: the type is unknown and `get` is noise → no edge, no guess
    assert ("pkg.cache::Cache::get", 18) not in by and ("pkg.map::Map::get", 18) not in by
    # `plain.insert(2)`: untyped receiver, two candidates → declined, recorded as a miss
    assert not any(e.line == 22 and e.to_qualified_name.endswith("Table::insert") for e in edges)
    assert any(m.callee_name == "insert" and m.line == 22 for m in misses)
