"""Every Go call site is recorded — a generic instantiation
`NewArray[ColStr](…)` names its callee, a package-qualified call on a
repository package (`cht.New(t)`) is never language noise, and a receiver
that is a composite literal or an assertion (`(&T{}).M()`, `x.(*T).M()`)
types as T."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import (
    _receiver_text,
    collect_call_names,
    extract_edges_and_misses,
)
from server.code.domain.extract.go_receivers import GoReceiverTyper
from server.code.domain.extract.lang_go import go_module_index, go_package_context
from server.code.domain.extract.name_scope import gate_name_table

_FILES = {
    "go.mod": b"module example.com/m\n",
    "proto/col.go": (
        b"package proto\ntype ColStr struct{}\ntype ColArr[T any] struct{ data []T }\n"
        b"func NewArray[T any]() *ColArr[T] { return nil }\n"
        b"func (c *ColArr[T]) Append(v T) {}\n"
        b"type ColDateTime64 struct{}\nfunc (c ColDateTime64) WithPrecision(p int) ColDateTime64 { return c }\n"
        b"func Null[T any](v *T) T { return *v }\n"
    ),
    "cht/server.go": b"package cht\ntype Server struct{}\nfunc New(t any) *Server { return nil }\n",
    "app/main.go": (
        b'package main\nimport (\n\t"errors"\n\t"example.com/m/cht"\n\t"example.com/m/proto"\n)\n'
        b"func run(x any) {\n"
        b"\tarr := proto.NewArray[proto.ColStr]()\n"  # 8: generic, package-qualified
        b"\tarr.Append(proto.ColStr{})\n"  # 9: typed by the generic's return
        b"\tsrv := cht.New(nil)\n"  # 10: `New` is noise bare, not on a repository package
        b"\t_ = srv\n"
        b'\t_ = errors.New("x")\n'  # 12: a library's New stays out
        b"\t(&proto.ColDateTime64{}).WithPrecision(9)\n"  # 13: literal receiver
        b"\tproto.ColDateTime64{}.WithPrecision(3)\n"  # 14
        b"\tx.(*proto.ColDateTime64).WithPrecision(6)\n"  # 15: assertion receiver
        b"\t_ = proto.Null[int32](nil)\n"  # 16: generic with a builtin argument
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


def test_generic_and_package_qualified_calls_are_named() -> None:
    parsed, _table, _index = _module()
    names = collect_call_names(parsed["app/main.go"])
    assert {"NewArray", "Append", "WithPrecision", "Null"} <= names
    assert "New" in names  # cht.New: the repository package's, not noise


def test_literal_and_assertion_receivers_spell_their_type() -> None:
    pf = parse_file(
        "p/a.go",
        b"""package p
func f(x any) {
	(&T{}).M()
	T{}.M()
	x.(*st.T).M()
	(T{}).M()
	g().M()
}
""",
    )
    calls = []

    def walk(node) -> None:
        for c in node.named_children:
            if (
                c.type == "call_expression"
                and c.child_by_field_name("function").type == "selector_expression"
            ):
                calls.append(_receiver_text(c, pf.source))
            walk(c)

    walk(pf.tree.root_node)
    assert calls == ["T{}", "T{}", "st.T{}", "T{}", "g()"]  # a call receiver: its return
    typer = GoReceiverTyper(pf)
    assert typer.type_of(3, "T{}") == "T" and typer.type_of(5, "st.T{}") == "st.T"


def test_every_call_site_binds_or_is_recorded() -> None:
    parsed, table, index = _module()
    pf = parsed["app/main.go"]
    returns = {
        s.qualified_name: s.return_type for f in parsed.values() for s in f.symbols if s.return_type
    }
    ctx = {"go_packages": go_package_context(pf, index), "go_method_sigs": {}}
    callee_returns = {"proto.NewArray": returns["proto.proto::NewArray"]}
    edges, misses = extract_edges_and_misses(pf, table, None, callee_returns, ctx)
    bound = {(e.line, e.to_qualified_name, e.confidence) for e in edges}
    assert bound == {
        (8, "proto.proto::NewArray", 1.0),  # the generic instantiation, import-bound
        (9, "proto.proto::ColArr::Append", 0.9),  # arr typed by NewArray's return
        (10, "cht.cht::New", 1.0),  # New on a repository package
        (13, "proto.proto::ColDateTime64::WithPrecision", 0.9),
        (14, "proto.proto::ColDateTime64::WithPrecision", 0.9),
        (15, "proto.proto::ColDateTime64::WithPrecision", 0.9),
        (16, "proto.proto::Null", 1.0),
    }
    assert misses == []  # errors.New: a library's, declined without a record


def test_a_methods_return_is_spelled_in_the_receiver_types_package() -> None:
    """`ts := new(proto.Col).WithPrecision(9)` returns proto's Col — not a type
    qualified by the receiver TEXT (`new(proto.Col).Col`), which no package
    rung could place; `a.store.Get()` likewise takes `st`, the type's package."""
    pf = parse_file(
        "app/a.go",
        b"""package main
import (
	"example.com/m/proto"
	st "example.com/m/store"
)
type app struct{ store *st.Store }
func run(a *app) {
	ts := new(proto.Col).WithPrecision(9)
	ts.Append(1)
	r := a.store.Get()
	r.Close()
	s := st.NewStore()
	s.Put()
}
""",
    )
    typer = GoReceiverTyper(
        pf,
        {
            "new(proto.Col).WithPrecision": "Col",
            "a.store.Get": "Rows",
            "st.NewStore": "Store",
        },
    )
    typer.field_lookup = lambda t, f: "st.Store" if (t, f) == ("app", "store") else ""
    assert typer.type_of(9, "ts") == "proto.Col"
    assert typer.type_of(11, "r") == "st.Rows"
    assert typer.type_of(13, "s") == "st.Store"
