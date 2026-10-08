"""A receiver that is itself a call (`Conn(t).Do()`,
`(&ColStr{}).Array().Append()`) is typed by the callee's return; a package
written by its clause (`ch` for `…/ch-go`) is a known alias; an alias target
in a package the file does not import is placed by clause."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import callee_targets, extract_edges_and_misses
from server.code.domain.extract.go_implementors import go_clauses_of, type_alias_target
from server.code.domain.extract.go_receivers import GoReceiverTyper, go_value_spelling
from server.code.domain.extract.lang_go import go_module_index, go_package_context
from server.code.domain.extract.name_scope import gate_name_table


def test_value_spelling_covers_every_receiver_form() -> None:
    pf = parse_file(
        "p/a.go",
        b"""package p
func f(x any, c Cols) {
	a.store.Get()
	c[0].Time()
	(&T{}).M()
	x.(*st.T).M()
	new(proto.Col).M()
	Conn(t).Do()
	table.Block().Encode()
	Kind(3).String()
	NewArray[T]().Append()
	g()()
}
""",
    )
    seen: list[str] = []

    def walk(node) -> None:
        for ch in node.named_children:
            if ch.type == "call_expression":
                fn = ch.child_by_field_name("function")
                if fn is not None and fn.type == "selector_expression":
                    seen.append(go_value_spelling(fn.child_by_field_name("operand"), pf.source))
            walk(ch)

    walk(pf.tree.root_node)
    assert seen == [
        "a.store",
        "c[]",
        "T{}",
        "st.T{}",
        "new(proto.Col)",
        "Conn()",
        "table.Block()",
        "table",  # the outer call's receiver, then the inner call's own
        "Kind()",
        "NewArray()",
    ]


_FILES = {
    "go.mod": b"module example.com/ch-go\n",
    "client.go": (
        b"package ch\ntype Client struct{}\nfunc (c *Client) Do() {}\nfunc (c *Client) Ping() {}\n"
        b"func Dial() *Client { return nil }\n"
    ),
    "client_test.go": b"package ch\nfunc Conn(t any) *Client { return nil }\n",
    "chpool/pool.go": b"package chpool\ntype Client struct{}\nfunc (c *Client) Do() {}\n",
    "proto/col.go": (
        b"package proto\ntype ColStr struct{}\nfunc (c *ColStr) Array() *ColArr { return nil }\n"
        b"type ColArr struct{}\nfunc (c *ColArr) Append(v any) {}\n"
        b"type Other struct{}\nfunc (o *Other) Append(v any) {}\n"
    ),
    "query_test.go": (
        b'package ch\nimport "example.com/ch-go/proto"\nfunc use(t any) {\n'
        b"\tConn(t).Do()\n"  # 4: a call receiver, same-package helper
        b"\tcol := (&proto.ColStr{}).Array()\n"  # 5
        b"\tcol.Append(1)\n"  # 6: typed through the literal-receiver call's return
        b"}\n"
    ),
    # the package is written by its clause `ch`, not the path element `ch_go`
    "cmd/tool/main.go": (
        b'package main\nimport "example.com/ch-go"\nfunc main() {\n'
        b"\tc := ch.Dial()\n"  # 4
        b"\tc.Do()\n"  # 5: ch's Client::Do, not chpool's
        b"\tch.Dial().Ping()\n"  # 6: a qualified call receiver
        b"}\n"
    ),
}


def _module():
    parsed = {p: parse_file(p, src) for p, src in _FILES.items() if p.endswith(".go")}
    index = go_module_index({"go.mod": _FILES["go.mod"].decode()})
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return parsed, gate_name_table(table, "go"), index


def _derive(parsed, table, index, path):
    pf = parsed[path]
    packages = go_package_context(pf, index)
    ctx = {
        "go_packages": packages,
        "go_clauses": go_clauses_of(packages, table),
        "go_method_sigs": {},
    }
    returns = {
        s.qualified_name: s.return_type for f in parsed.values() for s in f.symbols if s.return_type
    }
    targets = callee_targets(pf, table, ctx)
    callee_returns = {c: returns[q] for c, q in targets.items() if q in returns}
    edges, misses = extract_edges_and_misses(pf, table, None, callee_returns or None, ctx)
    return ctx, targets, callee_returns, edges, misses


def test_a_call_receiver_is_typed_by_the_callees_return() -> None:
    parsed, table, index = _module()
    _ctx, targets, callee_returns, edges, misses = _derive(parsed, table, index, "query_test.go")
    assert targets["Conn"] == "root.ch::Conn" and callee_returns["Conn"] == "Client"
    assert targets["proto.ColStr{}.Array"] == "proto.proto::ColStr::Array"  # the shared spelling
    assert {(e.line, e.to_qualified_name, e.confidence) for e in edges} >= {
        (4, "root.ch::Client::Do", 0.9),  # Conn(t).Do(): ch's, not chpool's
        (6, "proto.proto::ColArr::Append", 0.9),  # col := (&proto.ColStr{}).Array()
    }
    assert [m.callee_name for m in misses] == []


def test_a_package_is_known_by_its_clause() -> None:
    parsed, table, index = _module()
    ctx, targets, callee_returns, edges, misses = _derive(parsed, table, index, "cmd/tool/main.go")
    assert ctx["go_clauses"] == {"ch": "root"}  # the path element says ch_go; the clause says ch
    assert callee_returns["ch.Dial"] == "Client"
    typer = GoReceiverTyper(parsed["cmd/tool/main.go"], callee_returns)
    typer._import_aliases.update(ctx["go_clauses"])
    assert typer.type_of(5, "c") == "ch.Client"  # qualified by the clause alias
    assert {(e.line, e.to_qualified_name, e.confidence) for e in edges} >= {
        (4, "root.ch::Dial", 1.0),
        (5, "root.ch::Client::Do", 0.9),
        (6, "root.ch::Client::Ping", 0.9),  # ch.Dial().Ping(): a qualified call receiver
    }
    assert [m.callee_name for m in misses] == []


def test_an_alias_target_in_an_unimported_package_is_placed_by_clause() -> None:
    files = {
        "go.mod": b"module example.com/m\n",
        "dynamic.go": b'package root\nimport "example.com/m/lib/chcol"\ntype Dynamic = chcol.Dynamic\n',
        "lib/chcol/variant.go": (
            b'package chcol\ntype Variant struct{}\nfunc (v Variant) Type() string { return "" }\n'
            b"type Dynamic = Variant\n"
        ),
        "lib/column/dynamic.go": b'package column\ntype Dynamic struct{}\nfunc (d Dynamic) Type() string { return "" }\n',
        "tests/dyn_test.go": (
            b'package tests\nimport "example.com/m"\nfunc use(x any) {\n'
            b"\tx.(root.Dynamic).Type()\n"  # 4: root.Dynamic → chcol.Dynamic → Variant::Type
            b"}\n"
        ),
    }
    parsed = {p: parse_file(p, src) for p, src in files.items() if p.endswith(".go")}
    index = go_module_index({"go.mod": files["go.mod"].decode()})
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    table = gate_name_table(table, "go")
    pf = parsed["tests/dyn_test.go"]
    packages = go_package_context(pf, index)
    aliases = {
        s.qualified_name: type_alias_target(s.signature)
        for f in parsed.values()
        for s in f.symbols
        if s.kind == "type" and type_alias_target(s.signature)
    }
    assert aliases == {"root.root::Dynamic": "chcol.Dynamic", "lib.chcol.chcol::Dynamic": "Variant"}
    ctx = {
        "go_packages": packages,
        "go_clauses": go_clauses_of(packages, table),
        "go_method_sigs": {},
        "go_type_aliases": aliases,
    }
    edges, misses = extract_edges_and_misses(pf, table, None, None, ctx)
    assert {(e.line, e.to_qualified_name, e.confidence) for e in edges} == {
        (4, "lib.chcol.chcol::Variant::Type", 0.9),  # never lib/column's Dynamic::Type
    }
    assert misses == []
