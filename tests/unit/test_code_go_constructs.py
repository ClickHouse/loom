"""Go construct coverage in the fast tier — package-scoped, complete symbols;
calls bound through imports and typed receivers; structural interface
satisfaction. The pins read the corpus
module `tests/fixtures/code_lanes/go/constructs/` the way the ingest does:
parse every file, build the name table from the module's symbols, resolve
each file's imports through the module index, and derive its edges."""

from __future__ import annotations

import os
import posixpath
from typing import Any

import pytest

from server.code.domain.extract import lang_go
from server.code.domain.extract.edges_treesitter import (
    Edge,
    callee_targets,
    extract_edges_and_misses,
)
from server.code.domain.extract.go_implementors import implementor_edges, interface_method_names
from server.code.domain.extract.go_receivers import GoReceiverTyper
from server.code.domain.extract.lang_go import (
    go_import_aliases,
    go_module_index,
    go_package_context,
    go_scope,
    parse_go,
)
from server.code.domain.extract.name_scope import gate_name_table, qualified_name_class

ROOT = posixpath.join(os.path.dirname(__file__), "..", "fixtures", "code_lanes", "go", "constructs")


def _files() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, _dirs, files in os.walk(ROOT):
        for f in files:
            full = posixpath.join(dirpath, f)
            out[posixpath.relpath(full, ROOT)] = open(full, "rb").read()
    return out


@pytest.fixture(scope="module")
def module() -> dict[str, Any]:
    files = _files()
    parsed = {p: parse_go(p, src) for p, src in files.items() if p.endswith(".go")}
    name_table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    index = go_module_index({p: files[p].decode() for p in files if p.endswith("go.mod")})
    returns_by_qn = {
        s.qualified_name: s.return_type
        for pf in parsed.values()
        for s in pf.symbols
        if s.return_type
    }
    sigs = {s.id: s.signature for pf in parsed.values() for s in pf.symbols}
    edges: dict[str, list[Edge]] = {}
    misses: dict[str, list[Any]] = {}
    gated = gate_name_table(name_table, "go")
    for path, pf in parsed.items():
        # the ingest's per-file context: alias -> package scope, the candidate
        # signatures it reads, and the callee return types keyed as written
        ctx = {"go_packages": go_package_context(pf, index), "go_method_sigs": sigs}
        targets = callee_targets(pf, gated, ctx)
        returns = {c: returns_by_qn[q] for c, q in targets.items() if q in returns_by_qn}
        es, ms = extract_edges_and_misses(pf, gated, None, returns or None, ctx)
        edges[path], misses[path] = es, ms
    return {"parsed": parsed, "table": name_table, "edges": edges, "misses": misses, "index": index}


def _sym(pf: Any, qn: str) -> Any:
    return next(s for s in pf.symbols if s.qualified_name == qn)


# ---- symbols ---------------------------------------------------------------


def test_scope_is_the_package_not_the_file(module: dict[str, Any]) -> None:
    store = module["parsed"]["store/store.go"]
    other = module["parsed"]["store/store_other.go"]
    test = module["parsed"]["store/store_test.go"]
    assert {s.qualified_name.split("::")[0] for s in store.symbols} == {"store.store"}
    assert {s.qualified_name.split("::")[0] for s in other.symbols} == {"store.store"}
    assert {s.qualified_name.split("::")[0] for s in test.symbols} == {"store.store"}
    assert go_scope("main.go", "main") == "root.main"
    assert go_scope("a/b.go", "b") == "a.b"
    # the name gate classes a Go definition as script (dotted scope, no `/`)
    assert qualified_name_class("store.store::Store::Get") == "script"
    assert qualified_name_class("root.main::main") == "script"


def test_every_declaration_kind_is_a_symbol(module: dict[str, Any]) -> None:
    store = module["parsed"]["store/store.go"]
    kinds = {(s.kind, s.qualified_name.split("::", 1)[1]) for s in store.symbols}
    assert {
        ("const", "Version"),
        ("const", "KindA"),
        ("const", "KindB"),
        ("const", "KindC"),
        ("var", "ErrMissing"),
        ("var", "handlers"),
        ("var", "defaultStore"),
        ("type", "Kind"),
        ("type", "Handler"),
        ("type", "Alias"),
        ("interface", "Reader"),
        ("interface", "Writer"),
        ("interface", "ReadWriter"),
        ("method", "Reader::Get"),
        ("method", "Reader::Has"),
        ("method", "Writer::Put"),
        ("struct", "Base"),
        ("struct", "Store"),
        ("struct", "Pair"),
        ("function", "Base::Describe"),
        ("function", "NewStore"),
        ("function", "Store::Get"),
        ("function", "Pair::First"),
        ("function", "Map"),
        ("function", "init"),
    } <= kinds
    # an interface's method signature has no body; a struct method does
    assert _sym(store, "store.store::Reader::Get").has_body is False
    assert _sym(store, "store.store::Store::Get").has_body is True
    # two inits are two functions
    inits = [s for s in store.symbols if s.name == "init"]
    assert len(inits) == 2 and len({s.id for s in inits}) == 2
    assert inits[0].signature.startswith("(init@L")


def test_signatures_carry_generics_results_docs_and_build_tags(module: dict[str, Any]) -> None:
    store = module["parsed"]["store/store.go"]
    assert _sym(store, "store.store::Pair").signature == "type Pair[K comparable, V any]"
    assert _sym(store, "store.store::Map").signature == "[T, U any](xs []T, f func(T) U) []U"
    assert _sym(store, "store.store::Alias").signature == "type Alias = Store"
    assert _sym(store, "store.store::Store::Get").signature == (
        "(ctx context.Context, key string) ([]byte, error)"
    )
    assert _sym(store, "store.store::Reader::Get").signature == (
        "(ctx context.Context, key string) ([]byte, error)"
    )
    assert _sym(store, "store.store::Reader").signature == "type Reader interface{Get,Has}"
    assert _sym(store, "store.store::ReadWriter").signature == (
        "type ReadWriter interface{+Reader,+Writer,+io.Closer}"  # the qualifier stays
    )
    assert _sym(store, "store.store::NewStore").return_type == "Store"  # `*Store`
    assert _sym(store, "store.store::Pair::First").return_type == "K"
    assert _sym(store, "store.store::Store::Get").return_type == ""  # ([]byte, error)
    assert _sym(store, "store.store::NewStore").doc_comment == "NewStore builds a store."
    assert _sym(store, "store.store::Version").doc_comment == "Version is the store's version."
    # embedded struct and interface types are heritage
    assert ("store.store::Store", "Base") in store.inherits
    assert ("store.store::ReadWriter", "Reader") in store.inherits
    # build-tag variants of one name are two definitions
    linux = _sym(module["parsed"]["store/store_other.go"], "store.store::platformName")
    windows = _sym(module["parsed"]["store/store_windows.go"], "store.store::platformName")
    assert linux.signature == "() string [go:build linux]"
    assert windows.signature == "() string [go:build windows]"
    assert linux.id != windows.id


def test_import_aliases_follow_go_rules() -> None:
    pf = parse_go(
        "x/x.go",
        b"""package x
import (
	"fmt"
	st "example.com/m/store"
	"github.com/jackc/pgx/v5"
	. "strings"
	_ "embed"
	"github.com/go-sql-driver/mysql"
)
""",
    )
    assert go_import_aliases(pf) == {
        "fmt": "fmt",
        "st": "example.com/m/store",
        "pgx": "github.com/jackc/pgx/v5",
        "mysql": "github.com/go-sql-driver/mysql",
    }


# ---- receivers and the ladder ----------------------------------------------


def test_receiver_typer_reads_every_binding_form() -> None:
    pf = parse_go(
        "p/p.go",
        b"""package p
import st "example.com/m/store"
type app struct{ store *st.Store; inner *box }
type box struct{ deep *st.Store }
func NewBox() *box { return &box{} }
func (a *app) run(s *st.Store, xs []st.Store) {
	b := &box{}
	c := new(st.Store)
	d := NewBox()
	e, _ := x.(*st.Store)
	var f st.Reader = s
	g := st.Pair[string, int]{}
	h := func() { var inner st.Store; _ = inner }
	_ = h
}
""",
    )
    t = GoReceiverTyper(pf)
    line = 7
    assert t.type_of(line, "a") == "app"
    assert t.type_of(line, "s") == "st.Store"
    assert t.type_of(line, "xs") == "st.Store"
    assert t.type_of(8, "b") == "box"
    assert t.type_of(9, "c") == "st.Store"
    assert t.type_of(10, "d") == "box"  # same-file NewBox returns box
    assert t.type_of(11, "e") == "st.Store"
    assert t.type_of(12, "f") == "st.Reader"
    assert t.type_of(13, "g") == "st.Pair"
    assert t.type_of(13, "a.store") == "st.Store"
    assert t.type_of(13, "a.inner.deep") == "st.Store"
    assert t.type_of(14, "inner") == ""  # the func literal's binding stays in its own scope
    assert t.type_of(13, "nobody") == ""


def test_calls_bind_through_imports_typed_receivers_and_interfaces(module: dict[str, Any]) -> None:
    bound = {
        (e.line, e.to_qualified_name, e.confidence)
        for e in module["edges"]["cmd/app/main.go"]
        if e.edge_kind == "calls"
    }
    src = module["parsed"]["cmd/app/main.go"].source.decode().splitlines()

    def line_of(text: str) -> int:
        return next(i + 1 for i, row in enumerate(src) if text in row)

    # a typed receiver: the struct field `store *st.Store` behind `a`
    assert (line_of("a.store.Has("), "store.store::Store::Has", 0.9) in bound
    assert (line_of("a.store.Put("), "store.store::Store::Put", 0.9) in bound
    # a package-qualified call binds to the imported package's own function
    assert (line_of('st.NewStore("main")'), "store.store::NewStore", 1.0) in bound
    assert (line_of("st.Map("), "store.store::Map", 1.0) in bound
    # a constructor's return type types its result (`s := st.NewStore("bg")`)
    assert (line_of('s.Has("z")'), "store.store::Store::Has", 0.9) in bound
    # a generic type's method, type arguments stripped
    assert (line_of("p.First()"), "store.store::Pair::First", 0.9) in bound
    # a call through an interface value binds to the interface's method signature
    assert (line_of("r.Get(ctx"), "store.store::Reader::Get", 0.9) in bound
    # a promoted method (Base embedded in Store, declared in another file):
    # the unique-name rung still binds it
    assert (line_of("a.store.Describe()"), "store.store::Base::Describe", 0.6) in bound
    # stdlib and dependency calls are neither edges nor misses
    misses = {m.callee_name for m in module["misses"]["cmd/app/main.go"]}
    assert misses == {"get"}  # a method value: beyond the tree
    assert not any(
        e.to_qualified_name.endswith(("Println", "Exit", "Fprintln", "Lock"))
        for e in module["edges"]["cmd/app/main.go"]
    )


def test_untyped_calls_prefer_the_concrete_method_over_the_signature(
    module: dict[str, Any],
) -> None:
    es = module["edges"]["store/store_test.go"]
    put = [e for e in es if e.to_qualified_name.endswith("::Put")]
    assert [(e.to_qualified_name, e.confidence) for e in put] == [("store.store::Store::Put", 0.9)]
    # `s := NewStore("t")` — the constructor's return type reaches the test file
    get = [e for e in es if e.to_qualified_name.endswith("::Get")]
    assert [(e.to_qualified_name, e.confidence) for e in get] == [("store.store::Store::Get", 0.9)]
    # `t.Run`, `t.Fatal`, `f.Fuzz`, `b.N`: testing's, declined
    assert {m.callee_name for m in module["misses"]["store/store_test.go"]} == set()


# ---- structural interface satisfaction --------------------------------------


def test_implementors_are_derived_from_method_sets(module: dict[str, Any]) -> None:
    inh = {
        (e.from_qualified_name, e.to_qualified_name, e.confidence)
        for e in module["edges"]["store/store.go"]
        if e.edge_kind == "inherits"
    }
    assert ("store.store::Store", "store.store::Reader", 0.8) in inh
    assert ("store.store::Store", "store.store::Writer", 0.8) in inh
    # ReadWriter embeds Reader, Writer and io.Closer: Store has Get/Has/Put/Close
    assert ("store.store::Store", "store.store::ReadWriter", 0.8) in inh
    # embedding is heritage at 1.0
    assert ("store.store::Store", "store.store::Base", 1.0) in inh
    assert ("store.store::ReadWriter", "store.store::Reader", 1.0) in inh
    # Base (Describe only) satisfies nothing; Pair satisfies nothing
    assert not any(f in ("store.store::Base", "store.store::Pair") for f, _t, c in inh if c == 0.8)


def test_interface_side_reaches_a_type_in_another_file_and_a_marker_matches_nothing() -> None:
    iface = parse_go(
        "api/api.go",
        b"""package api
type Runner interface { Run() error; Stop() }
type Marker interface{}
""",
    )
    impl = parse_go(
        "impl/impl.go",
        b"""package impl
type Job struct{}
func (j *Job) Run() error { return nil }
func (j *Job) Stop() {}
type Half struct{}
func (h Half) Run() error { return nil }
""",
    )
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in (iface, impl):
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    from_iface = implementor_edges(iface, table, Edge)
    assert [(e.from_qualified_name, e.to_qualified_name) for e in from_iface] == [
        ("impl.impl::Job", "api.api::Runner"),
    ]
    # the type's own file leaves a foreign interface to that interface's file
    assert implementor_edges(impl, table, Edge) == []
    assert interface_method_names("type Marker interface{}") == ([], [])


def test_promoted_methods_count_toward_satisfaction_within_a_file() -> None:
    pf = parse_go(
        "q/q.go",
        b"""package q
type Closer interface { Close() error }
type base struct{}
func (b base) Close() error { return nil }
type Wrapped struct{ base }
""",
    )
    table: dict[str, list[tuple[str, str, str]]] = {}
    for s in pf.symbols:
        table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    inh = {(e.from_qualified_name, e.to_qualified_name) for e in implementor_edges(pf, table, Edge)}
    assert ("q.q::base", "q.q::Closer") in inh
    assert ("q.q::Wrapped", "q.q::Closer") in inh  # Close promoted through the embedded base


def test_a_package_comment_is_the_packages_module_symbol() -> None:
    """`// Package proto …` above the clause is a `module` row
    named by the package, carrying the doc; a file without one adds nothing."""
    doc = parse_go(
        "proto/doc.go",
        b"""// Package proto implements the native protocol.
//
// Columns are typed slices.
package proto
""",
    )
    (mod,) = [s for s in doc.symbols if s.kind == "module"]
    assert mod.qualified_name == "proto.proto::proto" and mod.signature == "package proto"
    assert (
        mod.doc_comment
        == "Package proto implements the native protocol.\n\nColumns are typed slices."
    )
    plain = parse_go("proto/col.go", b"package proto\n\ntype Col struct{}\n")
    assert [s for s in plain.symbols if s.kind == "module"] == []
    directive = parse_go("proto/gen.go", b"//go:generate stringer\npackage proto\n")
    assert [s for s in directive.symbols if s.kind == "module"] == []


def test_lane_modules_stay_outside_the_extractor_stamp() -> None:
    """A Go lane change is a targeted backfill, never a heal."""
    from tests.unit.test_code_extractor_stamp import NON_EXTRACTOR_MODULES

    assert (
        "go_receivers.py" in NON_EXTRACTOR_MODULES and "go_implementors.py" in NON_EXTRACTOR_MODULES
    )
    assert lang_go.__name__ == "server.code.domain.extract.lang_go"
