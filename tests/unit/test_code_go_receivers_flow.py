"""The Go receiver typer follows value flow — a type
switch's alias per case, a range loop's element, an index into a typed
container, a copy / dereference / channel receive, a conversion to the
file's own type — and a method a struct does not define is promoted through
the types it embeds even when the struct is declared in another file, from
the struct row's signature (`type T struct{+Base}`)."""

from __future__ import annotations

from typing import Any

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.go_implementors import candidate_method_ids, struct_embeds
from server.code.domain.extract.go_receivers import GoReceiverTyper
from server.code.domain.extract.lang_go import go_module_index, go_package_context
from server.code.domain.extract.name_scope import gate_name_table

_FLOW = b"""package p
type Kind int
type Base struct{}
type T struct{ *Base; items []Item; m map[string]*Item }
func f(x any, t *T, ch chan Item, xs []Item) {
	switch v := x.(type) {
	case *T:
		v.Run()
	case Item, *Base:
		_ = v
	}
	for _, it := range t.items { it.Go() }
	for k, val := range t.m { _ = k; val.Go() }
	for it := range ch { it.Go() }
	first := t.items[0]
	first.Go()
	y := t
	y.Run()
	z := *t
	z.Run()
	w := <-ch
	w.Go()
	k := Kind(3)
	k.String()
	one, ok := t.m["a"]
	_ = ok
	one.Go()
	for _, e := range xs { e.Go() }
	a := b
	b := a
	_ = b
}
"""


def test_typer_follows_value_flow() -> None:
    t = GoReceiverTyper(parse_file("p/a.go", _FLOW))
    assert t.type_of(8, "v") == "T"  # the single-type case binds the alias
    assert t.type_of(10, "v") == ""  # a multi-type case leaves it the switched value
    assert t.type_of(12, "it") == "Item"  # range over a slice field: the element
    assert t.type_of(13, "val") == "Item"  # range over a map: the value
    assert t.type_of(14, "it") == ""  # a single name is an index or an element — unknown
    assert t.type_of(16, "first") == "Item"  # index into a typed slice
    assert t.type_of(18, "y") == "T"  # a copy
    assert t.type_of(20, "z") == "T"  # a dereference
    assert t.type_of(22, "w") == "Item"  # a channel receive
    assert t.type_of(24, "k") == "Kind"  # a conversion to this file's type
    assert t.type_of(27, "one") == "Item"  # `v, ok := m[k]`
    assert t.type_of(28, "e") == "Item"  # range over a parameter
    assert t.type_of(31, "b") == ""  # a reference cycle ends within the hop budget
    assert {"T", "Item", "Kind"} <= t.type_names()


def test_embedded_fields_are_field_rows_and_the_struct_signature_lists_them() -> None:
    pf = parse_file(
        "p/a.go",
        b"""package p
import "io"
type Base struct{}
type T struct{ *Base; io.Closer; items []Item }
type U struct{ n int }
""",
    )
    by_qn = {s.qualified_name: s for s in pf.symbols}
    assert by_qn["p.p::T"].signature == "type T struct{+Base,+io.Closer}"
    assert by_qn["p.p::U"].signature == "type U"  # no embeds: unchanged
    assert by_qn["p.p::T::Base"].kind == "field" and by_qn["p.p::T::Base"].signature == "Base"
    assert by_qn["p.p::T::Closer"].signature == "io.Closer"  # the qualifier stays
    assert struct_embeds(by_qn["p.p::T"].signature) == ["Base", "io.Closer"]
    assert struct_embeds(by_qn["p.p::U"].signature) == []
    assert struct_embeds("type Pair[K comparable, V any]") == []


_FILES = {
    "go.mod": b"module example.com/m\n",
    "core/base.go": b'package core\ntype Base struct{}\nfunc (b *Base) Describe() string { return "" }\n',
    "core/t.go": b"package core\ntype T struct{ *Base }\n",
    "core/other.go": b'package core\ntype Other struct{}\nfunc (o Other) Describe() string { return "o" }\n',
    "app/main.go": (
        b'package main\nimport "example.com/m/core"\n'
        b"func run(t *core.T) {\n\tt.Describe()\n\tt.Base.Describe()\n}\n"
    ),
}


def _module() -> tuple[dict[str, Any], dict[str, list[tuple[str, str, str]]], dict[str, Any]]:
    parsed = {p: parse_file(p, src) for p, src in _FILES.items() if p.endswith(".go")}
    index = go_module_index({"go.mod": _FILES["go.mod"].decode()})
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return parsed, gate_name_table(table, "go"), index


def test_a_method_is_promoted_through_a_struct_declared_in_another_file() -> None:
    parsed, table, index = _module()
    pf = parsed["app/main.go"]
    sigs = {s.id: s.signature for f in parsed.values() for s in f.symbols}
    embeds = {
        s.qualified_name: struct_embeds(s.signature)
        for f in parsed.values()
        for s in f.symbols
        if s.kind == "struct" and struct_embeds(s.signature)
    }
    assert embeds == {"core.core::T": ["Base"]}
    ctx = {
        "go_packages": go_package_context(pf, index),
        "go_method_sigs": sigs,
        "go_struct_embeds": embeds,
    }
    edges, misses = extract_edges_and_misses(pf, table, None, None, ctx)
    bound = {(e.line, e.to_qualified_name, e.confidence) for e in edges}
    # `Describe` is declared on Base and on Other: only the promotion through
    # T's embedded Base picks Base's, at the typed-receiver confidence
    assert bound == {(4, "core.core::Base::Describe", 0.9), (5, "core.core::Base::Describe", 0.9)}
    assert misses == []
    # without the row's embeds `t.Describe()` is ambiguous between the two
    # Describes; the explicit `t.Base.Describe()` still types through the field row
    edges, _ = extract_edges_and_misses(
        pf,
        table,
        None,
        None,
        {"go_packages": ctx["go_packages"], "go_method_sigs": sigs},
    )
    assert {(e.line, e.to_qualified_name) for e in edges} == {(5, "core.core::Base::Describe")}


def test_an_unqualified_type_is_the_files_own_packages() -> None:
    """`Client::Do` in package ch and in package chpool: a ch file's
    `conn.Do()` with `conn` typed `Client` binds ch's, never "ambiguous"."""
    files = {
        "go.mod": b"module example.com/m\n",
        "client.go": b"package ch\ntype Client struct{}\nfunc (c *Client) Do() {}\n",
        "client_test.go": b"package ch\nfunc Conn() *Client { return nil }\n",
        "query_test.go": b"package ch\nfunc use() {\n\tconn := Conn()\n\tconn.Do()\n}\n",
        "chpool/pool.go": b"package chpool\ntype Client struct{}\nfunc (c *Client) Do() {}\n",
    }
    parsed = {p: parse_file(p, src) for p, src in files.items() if p.endswith(".go")}
    index = go_module_index({"go.mod": files["go.mod"].decode()})
    table: dict[str, list[tuple[str, str, str]]] = {}
    for f in parsed.values():
        for s in f.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    table = gate_name_table(table, "go")
    pf = parsed["query_test.go"]
    returns = {s.qualified_name: s.return_type for f in parsed.values() for s in f.symbols}
    ctx = {"go_packages": go_package_context(pf, index), "go_method_sigs": {}}
    callee_returns = {"Conn": returns["root.ch::Conn"]}
    edges, misses = extract_edges_and_misses(pf, table, None, callee_returns, ctx)
    assert {(e.line, e.to_qualified_name, e.confidence) for e in edges} >= {
        (4, "root.ch::Client::Do", 0.9),
    }
    assert [m.callee_name for m in misses] == []


def test_the_ingest_reads_the_bound_types_struct_rows() -> None:
    parsed, table, _index = _module()
    ids = set(candidate_method_ids(parsed["app/main.go"], table))
    t_row = next(s for s in parsed["core/t.go"].symbols if s.qualified_name == "core.core::T")
    assert t_row.id in ids  # the struct row whose signature names the embeds
