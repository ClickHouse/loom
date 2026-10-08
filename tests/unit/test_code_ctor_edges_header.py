"""A member initializer finds its field's type in the class's header.

Parser tier: `Foo.cpp` holds `Foo::Foo(...) : field_(args), Base(args)` and
`Foo.h` holds the class body. The ingest resolves the header (the sibling
`<stem>.h`, then the `#include` targets the index holds), rebuilds its text
from its live chunks and hands the declarations in as `HeaderDecls`; the
walker consults them only for a name the source file does not declare — a
field, a base, a `using` alias for a container — and binds through the
constructor-edge ladder unchanged. A header the index does not hold leaves
the site declined.

Additive: with `header=None` every site, edge and miss of the constructor-edge
and remainder fixtures is unchanged (their tests still run unchanged).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.app.index import ingest_edges, ingest_headers
from server.code.app.index.ingest_headers import (
    header_context,
    header_text_of_chunks,
    headers_first,
    include_candidates,
)
from server.code.app.index.ingest_stats import FileIngestStats
from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.edges_treesitter import (
    CtorSite,
    HeaderDecls,
    _Aliases,
    _type_parts_of_text,
    collect_call_names,
    extract_edges_and_misses,
    is_constructor_name,
    remainder_ctor_sites,
)
from server.code.domain.extract.treesitter import parse_cpp, symbol_id

# ---- the two-file fixture ---------------------------------------------------------

_HEADER = b"""\
#pragma once
#include <Processors/Chunk.h>
#include <vector>

namespace DB
{

class Widget
{
public:
    Widget();
    explicit Widget(int size);
    Widget(int a, int b);
};

struct Base
{
    explicit Base(int);
};

using Widgets = std::vector<Widget>;
using WidgetPtr = std::shared_ptr<Widget>;
using WidgetPtrs = std::vector<WidgetPtr>;
using Item = Widget;

class Holder : public Base
{
public:
    Holder(int x, Widget w, const Widget & cw);
    void add(int n, Widget w);
    struct Impl
    {
        Impl(int n);
        Widget inner_;
    };

private:
    Widget widget_;
    Widget copied_;
    Widget moved_;
    Widget * ptr_;
    Widgets items_;
    WidgetPtrs ptrs_;
    Item item_;
    int n_;
};

}
"""

_SOURCE = b"""\
#include <Processors/Holder.h>

namespace DB
{

Widget::Widget() {}
Widget::Widget(int) {}
Widget::Widget(int, int) {}
Base::Base(int) {}

Holder::Holder(int x, Widget w, const Widget & cw)
    : Base(x)
    , widget_(3)
    , copied_(cw)
    , moved_(std::move(w))
    , item_(1, 2)
    , n_(x)
{
}

Holder::Impl::Impl(int n) : inner_(n) {}

void Holder::add(int n, Widget w)
{
    items_.emplace_back(n);
    items_.emplace_back(w);
    this->items_.emplace_back(1, 2);
    ptrs_.emplace_back(std::make_shared<Widget>(4));
    Widgets local;
    local.emplace_back();
    unknown_.emplace_back(1);
}

}
"""

W0 = symbol_id("DB::Widget::Widget", "()")
W1 = symbol_id("DB::Widget::Widget", "(int)")
W2 = symbol_id("DB::Widget::Widget", "(int, int)")
B1 = symbol_id("DB::Base::Base", "(int)")


def _pf():
    return parse_cpp("src/Processors/Holder.cpp", _SOURCE)


def _header() -> HeaderDecls:
    hd = HeaderDecls()
    hd.add_source("src/Processors/Holder.h", _HEADER)
    return hd


def _sites(header: HeaderDecls | None) -> dict[tuple[int, str], CtorSite]:
    return {(s.line, s.detail): s for s in remainder_ctor_sites(_pf(), header)}


# ---- the header's declarations ------------------------------------------------------


def test_header_decls_hold_fields_bases_and_aliases_by_qualified_class() -> None:
    hd = _header()
    assert hd.paths == ["src/Processors/Holder.h"]
    hit = hd.class_for("Holder")
    assert hit is not None and hit[0] == "DB::Holder"
    fields = hit[1]
    assert fields["widget_"] is not None and fields["widget_"].written == "Widget"
    assert fields["ptr_"] is not None and fields["ptr_"].plain is False
    assert fields["items_"] is not None and fields["items_"].container == "Widgets"
    assert fields["n_"] is not None and fields["n_"].written == ""  # a primitive
    # a nested class is its own body, reached by the owner's qualification
    impl = hd.class_for("Holder::Impl")
    assert impl is not None and impl[0] == "DB::Holder::Impl" and "inner_" in impl[1]
    # a bare name that several classes share is declined
    assert hd.class_for("Impl") is not None  # one Impl here
    hd.add_source("src/Other.h", b"namespace X { struct Impl { int a; }; }")
    assert hd.class_for("Impl") is None
    assert hd.class_for("Holder::Impl") is not None
    assert hd.bases_for("Holder") == ["Base"]
    assert hd.aliases.resolve_text("Widgets") == "std::vector<Widget>"
    # alias of alias: WidgetPtrs -> vector<WidgetPtr>; WidgetPtr is a template alias, chased no further
    assert hd.aliases.resolve_text("WidgetPtrs") == "std::vector<WidgetPtr>"
    assert hd.aliases.resolve_text("WidgetPtr") == "std::shared_ptr<Widget>"
    assert hd.aliases.resolve_text("Nope") is None


@pytest.mark.parametrize(
    "text, parts",
    [
        ("std::vector<Chunk>", ("vector", ["Chunk"])),
        ("const std::vector<DB::Chunk>", ("vector", ["DB::Chunk"])),
        ("std::map<int, Widget>", ("map", ["", "Widget"])),
        ("std::vector<Widget *>", ("vector", [""])),
        ("std::array<int, 4>", ("array", ["", ""])),
        ("std::vector<std::pair<int, Widget>>", ("vector", ["std::pair"])),
        ("Widget", ("Widget", [])),
        ("DB::Widget", ("Widget", [])),
        ("Chunk *", ("", [])),
        ("Chunk &", ("", [])),
        ("std::vector<Chunk>::iterator", ("", [])),
        ("", ("", [])),
    ],
)
def test_alias_text_parts(text: str, parts: tuple[str, list[str]]) -> None:
    assert _type_parts_of_text(text) == parts


def test_aliases_skip_template_aliases_and_decline_double_definitions() -> None:
    pf = parse_cpp(
        "a.h",
        b"template <typename T> using Vec = std::vector<T>;\n"
        b"using A = std::vector<Widget>;\nusing A = std::vector<Other>;\n"
        b"typedef std::list<Widget> L, *LP;\nusing P = Widget *;\n",
    )
    al = _Aliases()
    al.add_tree(pf.tree.root_node, pf.source)
    assert "Vec" not in al.table
    assert al.table["A"] is None and al.resolve_text("A") is None
    assert al.resolve_text("L") == "std::list<Widget>"
    assert "LP" not in al.table  # a pointer typedef declarator is not an alias name
    assert _type_parts_of_text(al.resolve_text("P") or "") == ("", [])


# ---- the sites, with and without the header --------------------------------------------


def test_without_the_header_every_header_field_site_is_declined() -> None:
    sites = _sites(None)
    assert sites[(12, "Base")].decline == "unresolved" and sites[(12, "Base")].need == "field"
    assert sites[(13, "widget_")] == CtorSite(
        13, "", 1, "member_init", "unresolved", "widget_", "", "field"
    )
    assert sites[(21, "inner_")].decline == "unresolved"
    assert sites[(25, "items_.emplace_back")].decline == "unresolved"
    assert sites[(25, "items_.emplace_back")].need == "field"
    # a local of an alias type the file does not declare: the alias is what is missing
    assert sites[(30, "local.emplace_back")].need == "alias"
    assert not any(s.via for s in sites.values())


def test_with_the_header_the_sites_name_the_field_types() -> None:
    sites = _sites(_header())
    assert sites[(12, "Base")] == CtorSite(12, "Base", 1, "base_init", "", "Base", "header", "")
    assert sites[(13, "widget_")] == CtorSite(
        13, "Widget", 1, "member_init", "", "widget_", "header", ""
    )
    # the copy guard reads the header field's type too
    assert sites[(14, "copied_")].decline == "copy"
    assert sites[(15, "moved_")].decline == "copy"
    # `Item item_` with `using Item = Widget`: the alias names the class
    assert sites[(16, "item_")] == CtorSite(
        16, "Widget", 2, "member_init", "", "item_", "header+alias", ""
    )
    assert (17, "n_") not in sites  # a primitive field is no site
    # the nested class's constructor, out of line, finds its own body
    assert sites[(21, "inner_")] == CtorSite(
        21, "Widget", 1, "member_init", "", "inner_", "header", ""
    )
    # a field container spelled through an alias: header field + header alias
    e = sites[(25, "items_.emplace_back")]
    assert (e.written, e.argc, e.decline, e.via) == ("Widget", 1, "", "header+alias")
    assert sites[(26, "items_.emplace_back")].decline == "copy"
    assert sites[(27, "this->items_.emplace_back")].written == "Widget"
    assert sites[(27, "this->items_.emplace_back")].argc == 2
    # an alias for a vector of shared_ptr: the element is a library type, nothing binds it
    assert sites[(28, "ptrs_.emplace_back")].written == "shared_ptr"
    # a local of an alias type: the alias alone
    assert sites[(30, "local.emplace_back")] == CtorSite(
        30, "Widget", 0, "emplace", "", "local.emplace_back", "alias", ""
    )
    # a receiver nothing declares stays declined
    assert sites[(31, "unknown_.emplace_back")].decline == "unresolved"


def test_the_file_own_declarations_win_over_the_header() -> None:
    src = _SOURCE.replace(
        b"Holder::Holder(int x, Widget w, const Widget & cw)\n",
        b"class Holder { Base widget_; };\nHolder::Holder(int x, Widget w, const Widget & cw)\n",
    )
    pf = parse_cpp("src/Processors/Holder.cpp", src)
    sites = {(s.line, s.detail): s for s in remainder_ctor_sites(pf, _header())}
    w = sites[(14, "widget_")]
    assert w.written == "Base" and w.via == ""


# ---- the ladder --------------------------------------------------------------------


def _ctor_edges(edges):
    return {
        (e.from_qualified_name, e.line): (e.to_symbol_id, e.confidence)
        for e in edges
        if e.edge_kind == "calls" and is_constructor_name(e.to_qualified_name)
    }


def test_header_sites_bind_through_the_ladder_and_confidence_is_the_ladder_own() -> None:
    hd = _header()
    pf = _pf()
    names = collect_call_names(pf, hd)
    assert {"Widget", "Base"} <= names
    edges, misses = extract_edges_and_misses(pf, {}, hd)
    ctor = _ctor_edges(edges)
    hh = "DB::Holder::Holder"
    add = "DB::Holder::add"
    assert ctor[(hh, 12)] == (
        B1,
        1.0,
    )  # Base(x): the base from the header, the overload in this file
    assert ctor[(hh, 13)] == (W1, 1.0)  # widget_(3)
    assert ctor[(hh, 16)] == (W2, 1.0)  # item_(1, 2) through `using Item = Widget`
    assert ctor[("DB::Holder::Impl::Impl", 21)] == (W1, 1.0)
    assert ctor[(add, 25)] == (W1, 1.0)
    assert ctor[(add, 27)] == (W2, 1.0)
    assert ctor[(add, 28)] == (W1, 1.0)  # the factory route inside the emplace argument
    assert ctor[(add, 30)] == (W0, 1.0)
    for line in (14, 15, 17):
        assert (hh, line) not in ctor, line
    assert (add, 26) not in ctor and (add, 31) not in ctor
    assert all(e.confidence <= 1.0 for e in edges)
    # the declines leave no miss row: nothing names a constructor to record one under
    assert not [m for m in misses if m.callee_name in {"Widget", "Base", "emplace_back"}]


def test_without_the_header_the_edges_are_exactly_the_remainder_set() -> None:
    edges_none, misses_none = extract_edges_and_misses(_pf(), {})
    ctor = _ctor_edges(edges_none)
    # only the factory inside the emplace argument binds without the header
    assert ctor == {("DB::Holder::add", 28): (W1, 1.0)}
    edges_hd, misses_hd = extract_edges_and_misses(_pf(), {}, _header())
    keys_none = {(e.edge_kind, e.from_symbol_id, e.to_symbol_id, e.line) for e in edges_none}
    keys_hd = {(e.edge_kind, e.from_symbol_id, e.to_symbol_id, e.line) for e in edges_hd}
    assert keys_none <= keys_hd  # additive
    # the header moves no miss: a declined header site leaves no row
    assert {(m.callee_name, m.line) for m in misses_none} == {
        (m.callee_name, m.line) for m in misses_hd
    }


def test_inherited_fields_come_from_the_base_class_body_in_the_headers_read() -> None:
    hd = HeaderDecls()
    hd.add_source(
        "src/IProcessor.h",
        b"namespace DB { using OutputPorts = std::list<OutputPort>;\n"
        b"class IProcessor { protected: OutputPorts outputs; }; }",
    )
    hd.add_source(
        "src/ISource.h", b"namespace DB { class ISource : public IProcessor { int x; }; }"
    )
    hd.add_source("src/Src.h", b"namespace DB { class Src : public ISource { int y; }; }")
    fields = hd.fields_for("Src")
    assert set(fields) == {"y", "x", "outputs"}
    pf = parse_cpp("src/Src.cpp", b"namespace DB { Src::Src(int h) { outputs.emplace_back(h); } }")
    sites = remainder_ctor_sites(pf, hd)
    assert [(s.written, s.via, s.decline) for s in sites] == [("OutputPort", "header+alias", "")]
    # a fourth level up is not reached
    hd.add_source("src/Root.h", b"namespace DB { class Root { int deep; }; }")
    hd.add_source("src/IProcessor2.h", b"namespace DB { class IProcessor : public Root { }; }")
    assert hd.class_for("IProcessor") is None  # declared twice now: declined
    assert "deep" not in hd.fields_for("Src")


def test_a_header_declaring_several_constructors_refuses_the_repo_wide_rung() -> None:
    hd = HeaderDecls()
    hd.add_source(
        "src/Step.h",
        b"""namespace DB {
class ITransformingStep {
public:
    ITransformingStep(int a, int b, bool c = true);
    ITransformingStep(const ITransformingStep &) = default;
};
class Sink { public: Sink(int x); };
class Tmpl { public: template <typename T> Tmpl(T t); Tmpl(); };
class Foo : public ITransformingStep { Sink sink_; Tmpl t_; };
}""",
    )
    assert hd.declared_ctors("ITransformingStep") == 2
    assert hd.declared_ctors("Sink") == 1 and hd.declared_ctors("Tmpl") == 2
    assert hd.declared_ctors("Nope") == 0
    pf = parse_cpp(
        "src/Foo.cpp",
        b"namespace DB {\nFoo::Foo(int x)\n    : ITransformingStep(x, x)\n    , sink_(x)\n    , t_(x) {}\n}\n",
    )
    # only the `= default` copy constructor is indexed so far (its header came first): a lone
    # candidate the ladder would have taken at 0.6 — refused, since the header declares two
    table = {
        "ITransformingStep": [("copy", "DB::ITransformingStep::ITransformingStep", "function")],
        "Sink": [("s1", "DB::Sink::Sink", "function")],
        "Tmpl": [("t0", "DB::Tmpl::Tmpl", "function")],
    }
    edges, misses = extract_edges_and_misses(pf, table, hd)
    assert _ctor_edges(edges) == {("DB::Foo::Foo", 4): ("s1", 0.6)}
    assert not [m for m in misses if m.callee_name in {"ITransformingStep", "Tmpl"}]
    # the same-file rung still binds by arity when the source defines the overloads
    src = (
        b"namespace DB {\nITransformingStep::ITransformingStep(int a, int b, bool c) {}\n"
        b"Foo::Foo(int x)\n    : ITransformingStep(x, x, true)\n    , sink_(x) {}\n}\n"
    )
    edges, _ = extract_edges_and_misses(parse_cpp("src/Foo.cpp", src), table, hd)
    ctor = _ctor_edges(edges)
    real = symbol_id("DB::ITransformingStep::ITransformingStep", "(int a, int b, bool c)")
    assert ctor[("DB::Foo::Foo", 4)] == (real, 1.0)


def test_repo_wide_rung_applies_to_a_header_type_defined_elsewhere() -> None:
    src = b"""\
namespace DB {
Foo::Foo(int x) : port_(x), ports_(1) {}
void Foo::grow() { ports_.emplace_back(2, 3); }
}
"""
    hdr = b"""\
namespace DB {
using OutputPorts = std::list<OutputPort>;
class Foo { OutputPort port_; OutputPorts ports_; };
}
"""
    hd = HeaderDecls()
    hd.add_source("src/Foo.h", hdr)
    pf = parse_cpp("src/Foo.cpp", src)
    one = {
        "OutputPort": [
            ("op1", "DB::OutputPort::OutputPort", "function"),
            ("opc", "DB::OutputPort", "class"),
        ]
    }
    edges, misses = extract_edges_and_misses(pf, one, hd)
    ctor = _ctor_edges(edges)
    assert ctor[("DB::Foo::Foo", 2)] == ("op1", 0.6)
    assert ctor[("DB::Foo::grow", 3)] == ("op1", 0.6)
    # several overloads, none pinned: declined `ambiguous` under the class's bare name
    two = {"OutputPort": one["OutputPort"] + [("op2", "DB::OutputPort::OutputPort", "function")]}
    edges, misses = extract_edges_and_misses(pf, two, hd)
    assert not _ctor_edges(edges)
    assert {(m.callee_name, m.disposition) for m in misses} >= {("OutputPort", "ambiguous")}


# ---- the ingest: resolving and reading the header -------------------------------------


def _chunks_rows(path: str, source: bytes) -> list[dict[str, Any]]:
    return [
        {
            "start_line": c.start_line,
            "end_line": c.end_line,
            "content": c.content,
            "context_description": "",
        }
        for c in chunk_file(parse_cpp(path, source), max_chars=6000)
    ]


def test_header_text_rebuilt_from_chunks_declares_what_the_header_declares() -> None:
    text = header_text_of_chunks(_chunks_rows("src/Processors/Holder.h", _HEADER))
    assert "body elided" not in text
    hd = HeaderDecls()
    hd.add_source("src/Processors/Holder.h", text.encode())
    hit = hd.class_for("Holder")
    assert hit is not None and set(hit[1]) == {
        "widget_",
        "copied_",
        "moved_",
        "ptr_",
        "items_",
        "ptrs_",
        "item_",
        "n_",
    }
    assert hd.bases_for("Holder") == ["Base"]
    assert hd.aliases.resolve_text("Widgets") == "std::vector<Widget>"
    # a method chunk nested in the skeleton is not repeated
    rows = _chunks_rows("src/Processors/Holder.h", _HEADER) + [
        {"start_line": 30, "end_line": 30, "content": "GARBAGE NESTED", "context_description": ""}
    ]
    assert "GARBAGE" not in header_text_of_chunks(rows)


class _Repo:
    """Holds the chunks of some paths; records what was read."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.reads: list[tuple[str, Any]] = []

    async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
        self.reads.append(("present", tuple(paths)))
        return {p for p in paths if p in self.files}

    async def chunks_of_path(
        self, org: str, ns: str, path: str, *, limit: int = 400
    ) -> list[dict[str, Any]]:
        self.reads.append(("chunks", path))
        return _chunks_rows(path, self.files[path]) if path in self.files else []

    async def name_candidates(self, org: str, ns: str, names: list[str]) -> dict[str, Any]:
        return {}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    ingest_headers._header_cache.clear()
    ingest_headers._sibling_includes.clear()


async def test_header_context_reads_the_sibling_first_and_stops_when_nothing_is_left() -> None:
    repo = _Repo(
        {
            "src/Processors/Holder.h": _HEADER,
            "src/Processors/Chunk.h": b"namespace DB { class Chunk {}; }",
        }
    )
    stats = FileIngestStats(path="src/Processors/Holder.cpp")
    hd = await header_context(repo, "o", "n", _pf(), "src/Processors/Holder.cpp", stats)
    # `unknown_` can be helped by no header, so after the sibling the include
    # list is walked: the one indexed include is read, and nothing else
    assert hd is not None and hd.paths == ["src/Processors/Holder.h", "src/Processors/Chunk.h"]
    assert stats.header_paths == tuple(hd.paths) and stats.headers_read == 2
    assert [r[1] for r in repo.reads if r[0] == "chunks"] == hd.paths
    # every site the file declines is one a header supplies: the sibling alone is read
    src2 = _SOURCE.replace(b"    unknown_.emplace_back(1);\n", b"")
    repo.reads.clear()
    stats2 = FileIngestStats(path="src/Processors/Holder.cpp")
    hd2 = await header_context(
        repo,
        "o",
        "n",
        parse_cpp("src/Processors/Holder.cpp", src2),
        "src/Processors/Holder.cpp",
        stats2,
    )
    assert hd2 is not None and hd2.paths == ["src/Processors/Holder.h"]
    # the pass cache: the sibling's text was read once for the whole pass
    assert stats2.headers_read == 0 and [r[0] for r in repo.reads] == ["present"]


async def test_header_context_walks_the_include_list_when_there_is_no_sibling() -> None:
    src = b"""#include <Processors/Holder.h>\nnamespace DB {\nHolder::Holder(int x, Widget w, const Widget & cw) : Base(x), widget_(x) {}\n}\n"""
    repo = _Repo({"src/Processors/Holder.h": _HEADER})
    stats = FileIngestStats(path="src/Processors/Impl/HolderImpl.cpp")
    pf = parse_cpp("src/Processors/Impl/HolderImpl.cpp", src)
    hd = await header_context(repo, "o", "n", pf, "src/Processors/Impl/HolderImpl.cpp", stats)
    assert hd is not None and hd.paths == ["src/Processors/Holder.h"]
    present = [r for r in repo.reads if r[0] == "present"]
    # the include's forms were tried nearest-first and only the indexed one read
    assert any("src/Processors/Holder.h" in r[1] for r in present)
    table = {
        "Widget": [("w1", "DB::Widget::Widget", "function")],
        "Base": [("b1", "DB::Base::Base", "function")],
    }
    edges, _ = extract_edges_and_misses(pf, table, hd)
    # Base declares one constructor: the lone candidate binds at 0.6; Widget declares three,
    # so a lone indexed candidate is refused (which overload it is, the table cannot say)
    assert {(k, v) for k, v in _ctor_edges(edges).items()} == {
        (("DB::Holder::Holder", 3), ("b1", 0.6))
    }


async def test_header_not_indexed_declines_and_a_header_or_python_file_asks_nothing() -> None:
    repo = _Repo({})
    stats = FileIngestStats(path="src/Processors/Holder.cpp")
    assert await header_context(repo, "o", "n", _pf(), "src/Processors/Holder.cpp", stats) is None
    assert stats.header_paths == () and stats.headers_read == 0
    edges, _ = extract_edges_and_misses(_pf(), {}, None)
    assert _ctor_edges(edges) == {("DB::Holder::add", 28): (W1, 1.0)}
    repo2 = _Repo({"src/Processors/Holder.h": _HEADER})
    # a header is never a source that asks; a file with nothing declined reads nothing
    assert (
        await header_context(
            repo2,
            "o",
            "n",
            parse_cpp("src/Processors/Holder.h", _HEADER),
            "src/Processors/Holder.h",
            stats,
        )
        is None
    )
    plain = parse_cpp("src/x.cpp", b"void f() { int a = 1; }")
    assert await header_context(repo2, "o", "n", plain, "src/x.cpp", stats) is None
    assert repo2.reads == []


async def test_reingesting_a_header_forgets_its_cached_text() -> None:
    repo = _Repo({"src/Processors/Holder.h": _HEADER})
    stats = FileIngestStats(path="src/Processors/Holder.cpp")
    await header_context(repo, "o", "n", _pf(), "src/Processors/Holder.cpp", stats)
    assert ("o", "n", "src/Processors/Holder.h") in ingest_headers._header_cache
    # the header's own edge stage (run after its chunks are rewritten) forgets it
    hstats = FileIngestStats(path="src/Processors/Holder.h")
    await ingest_edges._derive_edges(
        repo,
        "o",
        "n",
        parse_cpp("src/Processors/Holder.h", _HEADER),
        "src/Processors/Holder.h",
        hstats,
    )
    assert ("o", "n", "src/Processors/Holder.h") not in ingest_headers._header_cache
    ingest_headers._header_cache[("o", "n", "x.h")] = "t"
    ingest_headers.forget_header("o", "n", "x.h")
    assert ("o", "n", "x.h") not in ingest_headers._header_cache


def test_include_candidates_are_nearest_first_and_headers_go_first() -> None:
    assert include_candidates("src/Processors/Transforms/X.cpp", "Processors/Chunk.h") == [
        "src/Processors/Transforms/Processors/Chunk.h",
        "src/Processors/Processors/Chunk.h",
        "src/Processors/Chunk.h",
        "Processors/Chunk.h",
    ]
    assert include_candidates("src/a/b.cpp", "../c.h") == ["src/c.h", "c.h"]
    assert include_candidates("x.cpp", "y.h") == ["y.h"]
    assert headers_first(["a/z.cpp", "a/z.h", "b.py", "a/y.hpp"]) == [
        "a/z.h",
        "a/y.hpp",
        "a/z.cpp",
        "b.py",
    ]
