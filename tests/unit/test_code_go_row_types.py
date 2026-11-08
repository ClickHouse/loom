"""A Go receiver the tree cannot type from this file alone
is typed from the repository's rows — a package-level const or var declared
in another file (`ColumnTypeInt8.Array()`, the row `const ColumnTypeInt8
ColumnType`), and the element of a named container type (`c[i].Time()` with
`type ColDate32 []Date32` elsewhere). The parser carries a const group's
implied type into the row; an indexed receiver is spelled `c[]`."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.go_implementors import (
    candidate_method_ids,
    type_element,
    value_type,
)
from server.code.domain.extract.go_receivers import (
    GoReceiverTyper,
    go_receiver_chain,
    unbound_receiver_heads,
)
from server.code.domain.extract.lang_go import go_module_index, go_package_context
from server.code.domain.extract.name_scope import gate_name_table


def test_signature_readers() -> None:
    assert type_element("type ColDate32 []Date32") == "Date32"
    assert type_element("type Set map[string]*Item") == "Item"
    assert type_element("type Grid [][]Cell") == "Cell"
    assert type_element("type Rows [8]Row") == "Row"
    assert type_element("type Q chan *Msg") == "Msg"
    assert type_element("type M map[string][]proto.Col") == "proto.Col"  # the qualifier stays
    assert type_element("type Kind int") == ""  # not a container
    assert type_element("type Pair[K comparable, V any] struct{+Base}") == ""
    assert type_element("type Fn func(int) []Item") == ""  # a func, whatever it returns
    assert value_type("const KindA Kind") == "Kind"
    assert value_type("var Def *Client") == "Client"
    assert value_type("const X proto.Kind") == "proto.Kind"
    assert value_type("var Default") == ""  # untyped: the tree's composite literal, not the row


def test_a_const_groups_implied_type_reaches_the_row() -> None:
    pf = parse_file(
        "p/k.go",
        b"""package p
type Kind int
const (
	KindA Kind = iota
	KindB
	KindC
	Loose = 5
	Next
	Typed Kind = 9
)
var (
	Def Client
	x = 1
)
""",
    )
    sigs = {s.name: s.signature for s in pf.symbols}
    assert sigs["KindA"] == "const KindA Kind"
    assert sigs["KindB"] == "const KindB Kind" and sigs["KindC"] == "const KindC Kind"
    assert sigs["Loose"] == "const Loose"  # a new untyped value starts a new implicit type
    assert sigs["Next"] == "const Next"
    assert sigs["Typed"] == "const Typed Kind"
    assert sigs["Def"] == "var Def Client" and sigs["x"] == "var x"


def test_receiver_chain_spells_an_index_as_the_element() -> None:
    pf = parse_file(
        "p/r.go",
        b"""package p
func f(c Cols, m map[string]*Item) {
	c[0].Time()
	(c[1]).Time()
	m["a"].items[2].Go()
	c[0]().Run()
	x.(*T).Run()
}
""",
    )
    calls: list[str] = []

    def walk(node) -> None:
        for child in node.named_children:
            if child.type == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is not None and fn.type == "selector_expression":
                    calls.append(go_receiver_chain(fn.child_by_field_name("operand"), pf.source))
            walk(child)

    walk(pf.tree.root_node)
    assert calls == ["c[]", "c[]", "m[].items[]", "", ""]


_FILES = {
    "go.mod": b"module example.com/m\n",
    "proto/column.go": (
        b"package proto\ntype ColumnType string\n"
        b"func (c ColumnType) Array() ColumnType { return c }\n"
        b'const (\n\tColumnTypeNone ColumnType = ""\n\tColumnTypeInt8\n)\n'
        b"type Date32 int32\nfunc (d Date32) Time() int { return 0 }\ntype ColDate32 []Date32\n"
    ),
    # a same-package namesake of both methods: only a typed receiver binds
    "proto/other.go": (
        b"package proto\ntype Other struct{}\n"
        b"func (o Other) Array() int { return 0 }\nfunc (o Other) Time() int { return 0 }\n"
    ),
    "proto/use_test.go": (
        b"package proto\nfunc use(c ColDate32) {\n\tColumnTypeInt8.Array()\n\tc[0].Time()\n"
        b"\tfor _, d := range c {\n\t\td.Time()\n\t}\n\tx := c[1]\n\tx.Time()\n}\n"
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


def test_receivers_type_from_rows_of_other_files() -> None:
    parsed, table, index = _module()
    pf = parsed["proto/use_test.go"]
    typer = GoReceiverTyper(pf)
    assert unbound_receiver_heads(pf, typer) == {"ColumnTypeInt8"}  # `c`, `d`, `x` the tree types
    assert typer.type_of(4, "c[]") == "ColDate32"  # no rows yet: a named type is its own element
    values = {
        s.name: value_type(s.signature)
        for f in parsed.values()
        for s in f.symbols
        if s.kind in ("const", "var") and value_type(s.signature)
    }
    elems = {
        s.name: type_element(s.signature)
        for f in parsed.values()
        for s in f.symbols
        if s.kind == "type" and type_element(s.signature)
    }
    assert values == {"ColumnTypeNone": "ColumnType", "ColumnTypeInt8": "ColumnType"}
    assert elems == {"ColDate32": "Date32"}
    ctx = {
        "go_packages": go_package_context(pf, index),
        "go_method_sigs": {},
        "go_value_types": values,
        "go_type_elems": elems,
    }
    edges, misses = extract_edges_and_misses(pf, table, None, None, ctx)
    assert {(e.line, e.to_qualified_name, e.confidence) for e in edges} == {
        (3, "proto.proto::ColumnType::Array", 0.9),  # the const's row types it
        (4, "proto.proto::Date32::Time", 0.9),  # `c[0]`: the named slice's element
        (6, "proto.proto::Date32::Time", 0.9),  # range over the named slice
        (9, "proto.proto::Date32::Time", 0.9),  # `x := c[1]`
    }
    assert misses == []
    # without the rows every one of them is ambiguous with Other's namesakes
    edges, misses = extract_edges_and_misses(
        pf,
        table,
        None,
        None,
        {"go_packages": ctx["go_packages"], "go_method_sigs": {}},
    )
    assert edges == [] and sorted(m.callee_name for m in misses) == [
        "Array",
        "Time",
        "Time",
        "Time",
    ]


def test_the_ingest_reads_const_and_type_rows_for_the_file() -> None:
    parsed, table, _index = _module()
    ids = set(candidate_method_ids(parsed["proto/use_test.go"], table))
    rows = {s.qualified_name: s.id for f in parsed.values() for s in f.symbols}
    assert rows["proto.proto::ColumnTypeInt8"] in ids  # the const the receiver names
    assert rows["proto.proto::ColDate32"] in ids  # the parameter's named container type
    assert rows["proto.proto::ColumnTypeNone"] not in ids  # not named by this file


async def test_an_empty_module_index_is_not_cached(monkeypatch) -> None:
    """A first backfill may derive a .go file before its go.mod is indexed:
    the index is re-read on the next file instead of an empty one being
    served for the cache's ten minutes."""
    from server.code.app.index import ingest_resolve

    paths: list[str] = []

    class Repo:
        async def live_paths_named(self, org: str, ns: str, name: str) -> list[str]:
            return list(paths)

    async def text(repo, org: str, ns: str, path: str, max_bytes: int = 0) -> str:
        return "module example.com/m\n"

    monkeypatch.setattr(ingest_resolve, "_package_json_text", text)
    ingest_resolve._GO_MODULE_INDEX_CACHE.pop(("o", "ns-11140"))
    assert await ingest_resolve._go_module_index(Repo(), "o", "ns-11140") == []
    paths.append("go.mod")  # the go.mod lands
    index = await ingest_resolve._go_module_index(Repo(), "o", "ns-11140")
    assert index and index[0][0] == "example.com/m"
    paths.clear()  # now cached: the index outlives a transient empty listing
    assert await ingest_resolve._go_module_index(Repo(), "o", "ns-11140") == index
    ingest_resolve._GO_MODULE_INDEX_CACHE.pop(("o", "ns-11140"))
