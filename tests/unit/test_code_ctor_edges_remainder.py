"""Member initializers and emplace calls are constructor calls too.

Parser tier: `Ctor() : member_(args), Base(args)` binds each initializer whose
member's declared type (a plain value field of the same class body in the
file) or base class has indexed constructors; `container.emplace_back(args)`
/ `emplace` / `try_emplace` / `emplace_hint` bind the container's element
type from the container's declaration (a local, a parameter, a field). Both
go through the constructor-edge ladder (same-file by arity 1.0, unique repo-wide 0.6,
several overloads declined `ambiguous`). A type the file does not declare,
and a sole argument that copies a value of the constructed type, are
declined — no edge, no guess.

Compiler tier: the converter's factory rule runs from the factory occurrence
to the type token across line breaks (`std::make_shared<\\n    T>(...)`).

Additive: every edge and miss of the base constructor-edge fixture is unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.domain.extract.edges_treesitter import (
    CtorSite,
    _skipped_args,
    collect_call_names,
    extract_edges_and_misses,
    is_constructor_name,
    remainder_ctor_sites,
)
from server.code.domain.extract.treesitter import parse_cpp, symbol_id

# ---- fixture ---------------------------------------------------------------------

_SRC = b"""\
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <variant>
#include <vector>
namespace DB {
class Widget {
public:
    Widget();
    explicit Widget(int size);
    Widget(int a, int b);
};
Widget::Widget() {}
Widget::Widget(int) {}
Widget::Widget(int, int) {}
struct Base { explicit Base(int); };
Base::Base(int) {}
struct Settings { int level = 0; };
using Processors = std::vector<std::shared_ptr<Widget>>;
Widget makeWidget();
class Holder : public Base {
public:
    Holder(int x, Widget w, const Widget & cw, Settings settings_)
        : Base(x)
        , widget_(3)
        , cw_(cw)
        , moved_(std::move(w))
        , from_int_(x)
        , from_call_(makeWidget())
        , from_unknown_(unknown_name)
        , ptr_(std::make_unique<Widget>(1))
        , settings(settings_)
        , n_{2}
        , items_(3)
    {}
    void add(std::vector<Widget> & param, Widget w, int n) {
        std::vector<Widget> v;
        v.emplace_back(9);
        v.emplace_back(w);
        v.emplace_back(n);
        v.emplace_back();
        param.emplace_back(1, 2);
        this->items_.emplace_back(3);
        items_.emplace(items_.begin(), 4);
        items_.emplace(items_.begin(), w);
        map_.try_emplace(1, 5);
        map_.emplace(1, 5);
        opt_.emplace(6);
        var_.emplace<Widget>(7);
        std::vector<Widget> * pv = &v;
        pv->emplace_back(1);
        Processors procs;
        procs.emplace_back(w);
        std::vector<std::shared_ptr<Widget>> sp;
        sp.emplace_back(w);
        std::vector<Widget *> wp;
        wp.emplace_back(&w);
        std::set<Widget> st;
        st.emplace(1);
        st.emplace_hint(st.begin(), 1, 2);
        for (auto & q : v)
            v.emplace_back(q);
        inherited_.emplace_back(1);
        std::vector<Other> others;
        others.emplace_back(1);
        std::vector<Two> twos;
        twos.emplace_back(1);
    }
    Widget widget_;
    const Widget cw_;
    Widget * wp_;
    std::unique_ptr<Widget> ptr_;
    std::vector<Widget> items_;
    std::map<int, Widget> map_;
    std::optional<Widget> opt_;
    std::variant<int, Widget> var_;
    Settings settings;
    int n_;
    Widget moved_, from_int_, from_call_, from_unknown_;
};
Holder::Holder() : widget_(1), n_(0) {}
void Holder::other() { items_.emplace_back(1, 2); }
}
"""

_TABLE: dict[str, list[tuple[str, str, str]]] = {
    "Other": [("oid", "DB::Other::Other", "function"), ("ocls", "DB::Other", "class")],
    "Two": [
        ("t1", "DB::Two::Two", "function"),
        ("t2", "DB::Two::Two", "function"),
        ("tc", "DB::Two", "class"),
    ],
}
W0 = symbol_id("DB::Widget::Widget", "()")
W1 = symbol_id("DB::Widget::Widget", "(int)")
W2 = symbol_id("DB::Widget::Widget", "(int, int)")
B1 = symbol_id("DB::Base::Base", "(int)")


def _pf():
    return parse_cpp("src/all.cpp", _SRC)


def _sites() -> dict[tuple[int, str], CtorSite]:
    return {(s.line, s.detail): s for s in remainder_ctor_sites(_pf())}


# ---- sites: what the parse says before the ladder runs -------------------------------


def test_member_init_sites_name_the_field_type_from_the_class_body() -> None:
    sites = _sites()
    assert sites[(25, "Base")] == CtorSite(25, "Base", 1, "base_init", "", "Base")
    assert sites[(26, "widget_")] == CtorSite(26, "Widget", 1, "member_init", "", "widget_")
    # a sole argument that is a value of the member's type: a copy or a move
    assert sites[(27, "cw_")].decline == "copy"
    assert sites[(28, "moved_")].decline == "copy"
    # a sole argument of a different, declared type binds by arity
    assert sites[(29, "from_int_")] == CtorSite(29, "Widget", 1, "member_init", "", "from_int_")
    # a call, an undeclared name: the value's type is not in the file
    assert sites[(30, "from_call_")].decline == "unresolved"
    assert sites[(31, "from_unknown_")].decline == "unresolved"
    # a library-typed member is a site of that type (nothing indexed binds it);
    # the factory inside its argument is the existing factory route
    assert sites[(32, "ptr_")].written == "std::unique_ptr"
    assert sites[(33, "settings")].decline == "copy"
    assert sites[(35, "items_")].written == "std::vector"
    # a primitive member is no site at all
    assert (34, "n_") not in sites
    # the out-of-line constructor finds the class body elsewhere in the file
    assert sites[(82, "widget_")] == CtorSite(82, "Widget", 1, "member_init", "", "widget_")


def test_emplace_sites_take_the_element_type_from_the_container_declaration() -> None:
    sites = _sites()
    assert sites[(39, "v.emplace_back")] == CtorSite(
        39, "Widget", 1, "emplace", "", "v.emplace_back"
    )
    assert sites[(40, "v.emplace_back")].decline == "copy"  # emplace_back(w), w a Widget
    assert sites[(41, "v.emplace_back")].decline == ""  # emplace_back(n), n an int
    assert sites[(42, "v.emplace_back")].argc == 0  # default construction
    assert sites[(43, "param.emplace_back")] == CtorSite(
        43, "Widget", 2, "emplace", "", "param.emplace_back"
    )
    assert sites[(44, "this->items_.emplace_back")].written == "Widget"  # a field through this
    # vector::emplace(pos, args): the position is not forwarded
    assert sites[(45, "items_.emplace")] == CtorSite(
        45, "Widget", 1, "emplace", "", "items_.emplace"
    )
    assert sites[(46, "items_.emplace")].decline == "copy"  # emplace(pos, w)
    # map::try_emplace(key, args) constructs the VALUE from args
    assert sites[(47, "map_.try_emplace")] == CtorSite(
        47, "Widget", 1, "emplace", "", "map_.try_emplace"
    )
    # map::emplace(k, v) builds a pair; the value is copied in — declined
    assert sites[(48, "map_.emplace")].decline == "unresolved"
    assert sites[(49, "opt_.emplace")].written == "Widget"
    assert sites[(50, "var_.emplace<Widget>")] == CtorSite(
        50, "Widget", 1, "emplace", "", "var_.emplace<Widget>"
    )
    assert sites[(52, "pv->emplace_back")].written == "Widget"  # through a pointer
    # an alias container (the file's own `using` is read): the
    # element is a library type, a site of that type that nothing binds
    procs = sites[(54, "procs.emplace_back")]
    assert (procs.written, procs.decline, procs.via) == ("std::shared_ptr", "", "alias")
    # an element that is a library type: a site of that type, nothing binds it
    assert sites[(56, "sp.emplace_back")].written == "std::shared_ptr"
    # a pointer element: no value is constructed, no site
    assert (58, "wp.emplace_back") not in sites
    assert sites[(60, "st.emplace")] == CtorSite(60, "Widget", 1, "emplace", "", "st.emplace")
    assert sites[(61, "st.emplace_hint")] == CtorSite(
        61, "Widget", 2, "emplace", "", "st.emplace_hint"
    )
    # a range-for variable has no declaration the scope reads: declined
    assert sites[(63, "v.emplace_back")].decline == "unresolved"
    # a container declared in a base class the file does not hold: declined
    assert sites[(64, "inherited_.emplace_back")].decline == "unresolved"
    # an out-of-line method finds its class's fields
    assert sites[(83, "items_.emplace_back")] == CtorSite(
        83, "Widget", 2, "emplace", "", "items_.emplace_back"
    )


@pytest.mark.parametrize(
    "method, container, explicit, skipped",
    [
        ("emplace_back", "vector", False, 0),
        ("emplace_front", "deque", False, 0),
        ("emplace_back", "set", False, None),
        ("emplace_back", "map", False, None),
        ("emplace", "vector", False, 1),
        ("emplace", "set", False, 0),
        ("emplace", "optional", False, 0),
        ("emplace", "map", False, None),
        ("emplace", "Processors", False, None),
        ("try_emplace", "map", False, 1),
        ("try_emplace", "vector", False, None),
        ("emplace_hint", "set", False, 1),
        ("emplace_hint", "map", False, None),
        ("emplace", "variant", True, 0),
        ("push_back", "vector", False, None),
    ],
)
def test_forwarded_argument_rule(
    method: str, container: str, explicit: bool, skipped: int | None
) -> None:
    assert _skipped_args(method, container, explicit) == skipped


def test_collect_call_names_includes_the_declared_types() -> None:
    names = collect_call_names(_pf())
    assert {"Widget", "Base", "Other", "Two"} <= names


# ---- the ladder --------------------------------------------------------------------


def _ctor_edges(edges):
    return {
        (e.from_qualified_name, e.line): (e.to_symbol_id, e.confidence)
        for e in edges
        if e.edge_kind == "calls" and is_constructor_name(e.to_qualified_name)
    }


def test_sites_bind_through_the_ladder_and_declines_leave_no_trace() -> None:
    edges, misses = extract_edges_and_misses(_pf(), _TABLE)
    ctor = _ctor_edges(edges)
    hh = "DB::Holder::Holder"
    add = "DB::Holder::add"
    assert ctor[(hh, 25)] == (B1, 1.0)  # Base(x)
    assert ctor[(hh, 26)] == (W1, 1.0)  # widget_(3)
    assert ctor[(hh, 29)] == (W1, 1.0)  # from_int_(x)
    assert ctor[(hh, 32)] == (W1, 1.0)  # ptr_(std::make_unique<Widget>(1)): the factory route
    assert ctor[(hh, 82)] == (W1, 1.0)  # Holder::Holder() : widget_(1), out of line
    for line in (27, 28, 30, 31, 33, 34, 35):
        assert (hh, line) not in ctor, line
    assert ctor[(add, 39)] == (W1, 1.0)
    assert ctor[(add, 41)] == (W1, 1.0)
    assert ctor[(add, 42)] == (W0, 1.0)
    assert ctor[(add, 43)] == (W2, 1.0)
    assert ctor[(add, 44)] == (W1, 1.0)
    assert ctor[(add, 45)] == (W1, 1.0)
    assert ctor[(add, 47)] == (W1, 1.0)
    assert ctor[(add, 49)] == (W1, 1.0)
    assert ctor[(add, 50)] == (W1, 1.0)
    assert ctor[(add, 52)] == (W1, 1.0)
    assert ctor[(add, 60)] == (W1, 1.0)
    assert ctor[(add, 61)] == (W2, 1.0)
    assert ctor[("DB::Holder::other", 83)] == (W2, 1.0)
    # unique repo-wide constructor: the repo-wide rung's grade
    assert ctor[(add, 66)] == ("oid", 0.6)
    # two overloads elsewhere, nothing to narrow by: declined ambiguous, like the base ladder
    assert (add, 68) not in ctor
    two = [m for m in misses if m.callee_name == "Two"]
    assert len(two) == 1 and two[0].disposition == "ambiguous" and two[0].line == 68
    # declined sites: no edge and no miss under the container method or the type
    for line in (40, 46, 48, 54, 56, 58, 63, 64):
        assert (add, line) not in ctor, line
    assert not [m for m in misses if m.callee_name in {"emplace_back", "Widget", "Processors"}]
    # emplace_back stays noise for the ordinary walker: no miss rows under it
    # (a miss there would feed closure repair with a container method)
    assert not [m for m in misses if m.callee_name == "emplace_back"]


def test_the_base_ctor_fixture_is_additive() -> None:
    """Every edge and miss the base constructor-edge fixture produces is still
    produced; the two shapes it leaves as gaps bind with the remainder on."""
    from tests.unit.test_code_ctor_call_edges import _SRC as BASE_SRC
    from tests.unit.test_code_ctor_call_edges import _TABLE as BASE_TABLE

    pf = parse_cpp("src/all.cpp", BASE_SRC)
    edges, misses = extract_edges_and_misses(pf, BASE_TABLE)
    keys = {
        (e.edge_kind, e.from_qualified_name, e.to_qualified_name, e.line, e.confidence)
        for e in edges
    }
    w_int = "DB::Widget::Widget"
    # the base fixture's edges (from its own assertions), unchanged
    for fn, line in [
        ("DB::useDirect", 25),
        ("DB::useTemp", 26),
        ("DB::useBrace", 27),
        ("DB::useNew", 28),
        ("DB::useMakeShared", 29),
        ("DB::useMultiLine", 33),
        ("DB::useMakeUnique", 30),
        ("DB::useQualified", 31),
    ]:
        assert ("calls", fn, w_int, line, 1.0) in keys
    assert ("calls", "DB::useHolder", "DB::Holder::Holder", 38, 1.0) in keys
    assert ("calls", "DB::useOther", "DB::Other::Other", 39, 0.6) in keys
    # the two closed gaps
    assert ("calls", "DB::Holder::Holder", w_int, 17, 1.0) in keys
    assert ("calls", "DB::useEmplace", w_int, 42, 1.0) in keys
    # the remainder is the only addition: with it switched off the walker is
    # the base one, and the difference is exactly the two closed gaps — no other
    # edge moves, no miss is added or removed
    with patch(
        "server.code.domain.extract.edges_treesitter.remainder_ctor_sites",
        lambda pf, header=None: [],
    ):
        base_edges, base_misses = extract_edges_and_misses(pf, BASE_TABLE)
    base_keys = {
        (e.edge_kind, e.from_qualified_name, e.to_qualified_name, e.line, e.confidence)
        for e in base_edges
    }
    assert keys - base_keys == {
        ("calls", "DB::Holder::Holder", w_int, 17, 1.0),
        ("calls", "DB::useEmplace", w_int, 42, 1.0),
    }
    assert base_keys <= keys
    assert [(m.callee_name, m.line, m.disposition) for m in misses] == [
        (m.callee_name, m.line, m.disposition) for m in base_misses
    ]
    assert ("Two", "ambiguous") in {(m.callee_name, m.disposition) for m in misses}
    # a plain non-C++ parse is untouched
    assert remainder_ctor_sites(parse_cpp("x.cpp", b"")) == []


def test_non_constructor_edges_and_misses_are_unchanged() -> None:
    src = b"""\
namespace DB {
int helper() { return 1; }
struct Box { Box(int); std::vector<int> ints; void fill() { ints.emplace_back(helper() + other()); } };
Box::Box(int) {}
}
"""
    pf = parse_cpp("src/x.cpp", src)
    edges, misses = extract_edges_and_misses(pf, {})
    calls = sorted(
        (e.from_qualified_name, e.to_qualified_name, e.confidence)
        for e in edges
        if e.edge_kind == "calls"
    )
    assert calls == [("DB::Box::fill", "DB::helper", 1.0)]
    assert [(m.callee_name, m.disposition) for m in misses] == [("other", "unresolved")]
    # an int element: a site of nothing
    assert not [s for s in remainder_ctor_sites(pf) if s.written]


# ---- compiler tier: the converter across line breaks ----------------------------------

_SPEC = importlib.util.spec_from_file_location(
    "scip_to_delta_ctor_remainder",
    Path(__file__).resolve().parents[2] / "scripts/scip_harness/scip_to_delta.py",
)
scip_to_delta = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["scip_to_delta_ctor_remainder"] = scip_to_delta
_SPEC.loader.exec_module(scip_to_delta)

# scip-clang 0.4.0 on $S/r10/fx/use.cpp: the factory
# occurrence is a function reference on its line; the type reference sits on
# the NEXT line, where the single-line rule sees only indentation.
_W = "cxx . . $ DB/Widget#"
_W1 = "cxx . . $ DB/Widget#Widget(d4f767463ce0a6b3)."
_MS = "cxx . . $ std/make_shared(25752750ef11e821)."
_MU = "cxx . . $ std/make_unique(5cd499e8540a10bb)."
_ONE = "cxx . . $ DB/One#"

_USE_CPP = """\
#include "widget.h"
namespace DB {
int useDirect() { Widget w(4); return 0; }
int useMakeShared() { auto w = std::make_shared<Widget>(8); return 0; }
int useMultiLineType() {
    auto w = std::make_shared<
        Widget>(1);
    return 0;
}
int useMultiLineArgs() {
    auto w = std::make_unique<Widget>(
        Config{},
        "x");
    return 0;
}
int useMultiLineQualified() {
    auto o = std::make_shared<
        DB::One
    >();
    return 0;
}
int useFarType() {
    std::shared_ptr<Widget> p = std::make_shared<Widget>(1); std::vector<int> v(
        Widget(1).run());
    return 0;
}
}
"""


def _occ(rng, symbol, roles=0):
    return NS(range=list(rng), symbol=symbol, symbol_roles=roles, enclosing_range=[])


def _at(ln: int, token: str, nth: int = 0):
    line = _USE_CPP.splitlines()[ln]
    col = -1
    for _ in range(nth + 1):
        col = line.index(token, col + 1)
    return [ln, col, col + len(token)]


def _docs():
    def fn(ln, name):
        return _occ(_at(ln, name), f"cxx . . $ DB/{name}(b126dc7c1de90089).", 1)

    use = NS(
        relative_path="use.cpp",
        symbols=[],
        occurrences=[
            fn(2, "useDirect"),
            _occ(_at(2, "Widget"), _W),
            _occ(_at(2, "w"), _W1),
            fn(3, "useMakeShared"),
            _occ(_at(3, "make_shared"), _MS),
            _occ(_at(3, "Widget"), _W),
            fn(4, "useMultiLineType"),
            _occ(_at(5, "make_shared"), _MS),
            _occ(_at(6, "Widget"), _W),
            fn(9, "useMultiLineArgs"),
            _occ(_at(10, "make_unique"), _MU),
            _occ(_at(10, "Widget"), _W),
            fn(15, "useMultiLineQualified"),
            _occ(_at(16, "make_shared"), _MS),
            _occ(_at(17, "One"), _ONE),
            # a type token on the line after a factory that is NOT its template
            # argument (the factory's own argument list closed on its line)
            fn(21, "useFarType"),
            _occ(_at(22, "Widget"), _W),
            _occ(_at(22, "make_shared"), _MS),
            _occ(_at(22, "Widget", 1), _W),
            _occ(_at(23, "Widget"), _W),
            _occ(_at(23, "Widget"), _W1),
        ],
    )
    return [use]


class _PB:
    class Document:
        def ParseFromString(self, payload):
            raise AssertionError("streaming is patched in this test")


def _convert(tmp_path: Path) -> dict:
    root = tmp_path / "checkout"
    root.mkdir(exist_ok=True)
    (root / "use.cpp").write_text(_USE_CPP)
    (tmp_path / "index.scip").write_bytes(b"")
    docs = _docs()
    with (
        patch.object(scip_to_delta, "_ensure_scip_pb2", return_value=_PB),
        patch.object(scip_to_delta, "_stream_documents", lambda data, pb, **_: iter(docs)),
        patch.object(scip_to_delta, "_top_level_records", lambda data: iter([])),
        patch.object(
            scip_to_delta,
            "_ctor_overloads",
            lambda records, pb: {
                "DB::Widget": [
                    "(scip:49f6e7a06ebc5aa8)",
                    "(scip:d4f767463ce0a6b3)",
                    "(scip:e8b05e15c683b320)",
                ],
                "DB::One": ["(scip:49f6e7a06ebc5aa8)"],
            },
        ),
    ):
        return scip_to_delta.convert(
            str(tmp_path / "index.scip"),
            None,
            [],
            "c0ffee",
            "",
            source_root=str(root),
            full=True,
        )


def test_converter_binds_the_type_on_the_line_after_the_factory(tmp_path: Path) -> None:
    delta = _convert(tmp_path)
    got = sorted(
        (
            e["from_qualified_name"],
            e["to_qualified_name"],
            e["to_signature"],
            e["line"],
            e.get("confidence", 1.0),
        )
        for f in delta["files"]
        for e in f["edges"]
        if e["to_qualified_name"] in {"DB::Widget::Widget", "DB::One::One"}
    )
    by_fn: dict[str, list] = {}
    for g in got:
        by_fn.setdefault(g[0], []).append(g)
    # same-line shapes: exactly as the single-line rule emits them
    assert by_fn["DB::useDirect"] == [
        ("DB::useDirect", "DB::Widget::Widget", "(scip:d4f767463ce0a6b3)", 3, 1.0)
    ]
    assert [g[3:] for g in by_fn["DB::useMakeShared"]] == [(4, 0.8)] * 3
    assert [g[3:] for g in by_fn["DB::useMultiLineArgs"]] == [(11, 0.8)] * 3
    # the type on the next line: bound, on the factory's line (where the call begins)
    assert [g[3:] for g in by_fn["DB::useMultiLineType"]] == [(6, 0.8)] * 3
    assert by_fn["DB::useMultiLineQualified"] == [
        ("DB::useMultiLineQualified", "DB::One::One", "(scip:49f6e7a06ebc5aa8)", 17, 1.0)
    ]
    # a factory on the previous line whose argument list is closed: only the
    # same-line factory binds, and the temporary `Widget(1)` is the direct edge
    far = by_fn["DB::useFarType"]
    assert [g[3:] for g in far if g[4] == 0.8] == [(23, 0.8)] * 3
    assert ("DB::useFarType", "DB::Widget::Widget", "(scip:d4f767463ce0a6b3)", 24, 1.0) in far
    assert delta["stats"]["factory_ctor_edges"] == 13 and delta["features"] == ["ctor_calls"]


def test_span_rule_shapes() -> None:
    f = scip_to_delta._factory_type_ref_span
    lines = [
        "auto w = std::make_shared<",
        "    Widget>(1);",
        "x",
        "auto p = make_unique<const",
        "  DB::Widget>();",
    ]
    ends = [(0, len("auto w = std::make_shared")), (3, len("auto p = make_unique"))]
    assert f(lines, [1, 4, 10], ends) == 0
    assert f(lines, [4, 6, 12], ends) == 3  # qualifier and cv across the break
    assert f(lines, [2, 0, 1], ends) is None  # nothing factory-shaped between
    assert f(None, [1, 4, 10], ends) is None and f(lines, [1, 4, 10], []) is None
    # a factory whose own line already closed the template list: not this token's
    assert f(["auto a = std::make_shared<Widget>(1);", "Widget w;"], [1, 0, 6], [(0, 24)]) is None
    # too far away
    far_lines = ["std::make_shared<"] + [""] * 20 + ["Widget>()"]
    assert f(far_lines, [21, 0, 6], [(0, 16)]) is None
