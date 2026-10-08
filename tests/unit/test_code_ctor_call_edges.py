"""A constructor call is a call.

Parser tier: `T x(...)`, `T x{...}`, `T(...)`, `new T(...)`, `T{...}` and the
factories `std::make_shared<T>` / `std::make_unique<T>` become `calls` edges
to `T::T`, bound by the existing ladder (same-file 1.0 narrowed by arity,
unique repo-wide 0.6, several overloads declined as ambiguous).

Compiler tier: the converter turns the type reference under a factory into
one edge per known constructor overload (1.0 for a single constructor, 0.8
each for several) and a FULL index claims the `ctor_calls` feature, which the
apply records in code_index_state.

Read side: a constructor seed with an empty walk is `lower_bound` with cause
`ctor_edges_absent` until the tier answering for it carries the edges.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.app.symbol import dispatch, verify
from server.code.app.ttl_cache import clear_all
from server.code.domain.extract.edges_treesitter import (
    _arity_ok,
    collect_call_names,
    ctor_sites,
    extract_edges_and_misses,
    is_constructor_name,
)
from server.code.domain.extract.treesitter import parse_cpp, symbol_id
from server.code.domain.index import epistemic as ep
from server.settings import settings

# ---- name shape -------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("DB::Widget::Widget", True),
        ("Widget::Widget", True),
        ("DB::ConcurrentHashJoin::ConcurrentHashJoin(scip:ab)", True),
        ("DB::Widget::~Widget", False),
        ("DB::Widget::run", False),
        ("Widget", False),
        ("", False),
        ("::", False),
    ],
)
def test_constructor_name_shape(name: str, expected: bool) -> None:
    assert is_constructor_name(name) is expected


@pytest.mark.parametrize(
    "sig, argc, ok",
    [
        ("()", 0, True),
        ("()", 1, False),
        ("(void)", 0, True),
        ("(int size)", 1, True),
        ("(int size)", 0, False),
        ("(const Config & cfg, std::string name)", 2, True),
        ("(const Config & cfg, std::string name)", 1, False),
        ("(int a, int b = 1)", 1, True),
        ("(int a, int b = 1)", 2, True),
        ("(int a, int b = 1)", 3, False),
        ("(std::map<int, int> m)", 1, True),  # comma inside <> is not a separator
        ("(Args &&... args)", 0, True),
        ("(Args &&... args)", 5, True),
        ("(int first, Args &&... rest)", 0, False),
        ("(int first, Args &&... rest)", 3, True),
        ("(scip:d4f767463ce0a6b3)", 7, True),  # the compiler hash admits any count
    ],
)
def test_arity_ladder(sig: str, argc: int, ok: bool) -> None:
    assert _arity_ok(sig, argc) is ok


# ---- parser tier -----------------------------------------------------------------

_SRC = b"""\
#include <memory>
#include <string>
#include <vector>
namespace DB {
struct Config { int level = 0; };
class Widget {
public:
    Widget();
    explicit Widget(int size);
    Widget(const Config & cfg, std::string name);
    int run() const;
private:
    int size_;
};
class Holder {
public:
    Holder() : widget_(3), config_() {}
    Widget widget_;
    Config config_;
};
struct Plain { int a; int b; };
Widget::Widget() : size_(0) {}
Widget::Widget(int size) : size_(size) {}
Widget::Widget(const Config & cfg, std::string) : size_(cfg.level) {}
int useDirect() { Widget w(4); return 0; }
int useTemp() { return Widget(5).run(); }
int useBrace() { Widget w{6}; return 0; }
int useNew() { auto * w = new Widget(7); delete w; return 0; }
int useMakeShared() { auto w = std::make_shared<Widget>(8); return 0; }
int useMakeUnique(const Config & c) { auto w = std::make_unique<Widget>(c, "x"); return 0; }
int useQualified() { auto w = std::make_shared<DB::Widget>(); return 0; }
int useMultiLine() {
    auto w = std::make_shared<Widget>(
        1);
    return 0;
}
int useAggregate() { Plain p{1, 2}; return p.a; }
int useHolder() { Holder h; return 0; }
int useOther() { auto o = std::make_shared<Other>(1); return 0; }
int useTwo() { auto t = std::make_unique<Two>(2); return 0; }
int useLibrary() { std::string s("x"); std::vector<int> v(3); return 0; }
int useEmplace() { std::vector<Widget> v; v.emplace_back(9); return 0; }
}
"""

_TABLE: dict[str, list[tuple[str, str, str]]] = {
    # a class elsewhere with exactly one constructor: unique repo-wide
    "Other": [("oid", "DB::Other::Other", "function"), ("ocls", "DB::Other", "class")],
    # two constructors elsewhere, no parameter lists in the table: ambiguous
    "Two": [
        ("t1", "DB::Two::Two", "function"),
        ("t2", "DB::Two::Two", "function"),
        ("tc", "DB::Two", "class"),
    ],
}


def _pf():
    return parse_cpp("src/all.cpp", _SRC)


def test_sites_name_the_type_and_count_arguments() -> None:
    sites = {(ln, t, n) for ln, t, n in ctor_sites(_pf())}
    assert (25, "Widget", 1) in sites  # Widget w(4)
    assert (27, "Widget", 1) in sites  # Widget w{6}
    assert (28, "Widget", 1) in sites  # new Widget(7)
    assert (29, "Widget", 1) in sites  # make_shared<Widget>(8)
    assert (30, "Widget", 2) in sites  # make_unique<Widget>(c, "x")
    assert (31, "DB::Widget", 0) in sites  # make_shared<DB::Widget>()
    assert (33, "Widget", 1) in sites  # multi-line factory: the call's first line
    assert (37, "Plain", 2) in sites
    assert (38, "Holder", 0) in sites  # `Holder h;` is a default construction
    assert (41, "std::string", 1) in sites and (41, "std::vector", 1) in sites
    # `Widget(5)` is a call node, not a type site; the walker routes it by name
    assert not any(ln == 26 for ln, _, _ in sites)
    # `std::vector<Widget> v;` names vector, not Widget
    assert (42, "std::vector", 0) in sites and (42, "Widget", 0) not in sites


def test_collect_call_names_includes_constructed_types() -> None:
    names = collect_call_names(_pf())
    assert {"Widget", "Other", "Two", "Plain", "Holder", "string", "vector"} <= names


def test_every_shape_binds_to_the_constructor_by_arity() -> None:
    edges, misses = extract_edges_and_misses(_pf(), _TABLE)
    ctor = {
        (e.from_qualified_name, e.line): (e.to_qualified_name, e.to_symbol_id, e.confidence)
        for e in edges
        if e.edge_kind == "calls" and is_constructor_name(e.to_qualified_name)
    }
    w_int = symbol_id("DB::Widget::Widget", "(int size)")
    w_cfg = symbol_id("DB::Widget::Widget", "(const Config & cfg, std::string)")
    w_default = symbol_id("DB::Widget::Widget", "()")
    for fn, line in [
        ("DB::useDirect", 25),
        ("DB::useTemp", 26),
        ("DB::useBrace", 27),
        ("DB::useNew", 28),
        ("DB::useMakeShared", 29),
        ("DB::useMultiLine", 33),
    ]:
        assert ctor[(fn, line)] == ("DB::Widget::Widget", w_int, 1.0), (
            fn,
            line,
            ctor.get((fn, line)),
        )
    assert ctor[("DB::useMakeUnique", 30)] == ("DB::Widget::Widget", w_cfg, 1.0)
    assert ctor[("DB::useQualified", 31)] == ("DB::Widget::Widget", w_default, 1.0)
    assert ctor[("DB::useHolder", 38)] == (
        "DB::Holder::Holder",
        symbol_id("DB::Holder::Holder", "()"),
        1.0,
    )
    # a member-init list (`Holder() : widget_(3)`): the field's declared type in
    # the same class body binds it
    assert ctor[("DB::Holder::Holder", 17)] == ("DB::Widget::Widget", w_int, 1.0)
    # unique repo-wide constructor: the repo-wide rung's grade
    assert ctor[("DB::useOther", 39)] == ("DB::Other::Other", "oid", 0.6)
    # two overloads, no parameter lists to narrow by: declined as ambiguous, not guessed
    assert ("DB::useTwo", 40) not in ctor
    two = [m for m in misses if m.callee_name == "Two"]
    assert len(two) == 1 and two[0].disposition == "ambiguous" and two[0].candidates == 2
    assert two[0].from_qualified_name == "DB::useTwo" and two[0].line == 40
    # an aggregate and library types have no indexed constructor: no edge, no miss
    assert ("DB::useAggregate", 37) not in ctor and ("DB::useLibrary", 41) not in ctor
    assert not [m for m in misses if m.callee_name in {"Plain", "string", "vector", "Widget"}]
    # additive: the factory call's own (pre-existing) unresolved miss is untouched —
    # a factory can be a repo function with callers of its own (DB::make_intrusive)
    assert [m for m in misses if m.callee_name == "make_shared<Other>"]
    # `std::vector<Widget> v; v.emplace_back(9)`: the container's element type
    # binds it
    assert ctor[("DB::useEmplace", 42)] == ("DB::Widget::Widget", w_int, 1.0)


def test_non_constructor_edges_are_unchanged() -> None:
    src = b"""\
namespace DB {
template <typename T, typename... Args> T * make_intrusive(Args &&... args);
int helper() { return 1; }
int caller() { return helper() + other(); }
int viaFactory() { auto * p = make_intrusive<Widget>(1); return 0; }
}
"""
    pf = parse_cpp("src/x.cpp", src)
    table = {
        "Widget": [("wid", "DB::Widget::Widget", "function")],
        "make_intrusive": [("mi", "DB::make_intrusive", "function")],
    }
    edges, misses = extract_edges_and_misses(pf, table)
    calls = sorted(
        (e.from_qualified_name, e.to_qualified_name, e.confidence)
        for e in edges
        if e.edge_kind == "calls"
    )
    # the ordinary edges, PLUS the construction — the factory keeps its callers
    assert calls == [
        ("DB::caller", "DB::helper", 1.0),
        ("DB::viaFactory", "DB::Widget::Widget", 0.6),
        ("DB::viaFactory", "DB::make_intrusive", 0.6),
    ]
    assert [(m.callee_name, m.disposition) for m in misses] == [("other", "unresolved")]


# ---- compiler tier: the converter ----------------------------------------------------

_SPEC = importlib.util.spec_from_file_location(
    "scip_to_delta_ctor_edges",
    Path(__file__).resolve().parents[2] / "scripts/scip_harness/scip_to_delta.py",
)
scip_to_delta = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["scip_to_delta_ctor_edges"] = scip_to_delta
_SPEC.loader.exec_module(scip_to_delta)

# Occurrences as scip-clang 0.4.0 emits them for the fixture above: a direct
# construction carries a reference to the constructor
# symbol; a factory carries the factory call plus a TYPE reference at the
# template argument and nothing for the constructor.
_W = "cxx . . $ DB/Widget#"
_W0 = "cxx . . $ DB/Widget#Widget(49f6e7a06ebc5aa8)."
_W1 = "cxx . . $ DB/Widget#Widget(d4f767463ce0a6b3)."
_W2 = "cxx . . $ DB/Widget#Widget(e8b05e15c683b320)."
_MS = "cxx . . $ std/make_shared(25752750ef11e821)."
_ONE = "cxx . . $ DB/One#"
_ONE0 = "cxx . . $ DB/One#One(49f6e7a06ebc5aa8)."

_USE_CPP = """\
#include "widget.h"
namespace DB {
int useDirect() { Widget w(4); return 0; }
int useMakeShared() { auto w = std::make_shared<Widget>(8); return 0; }
int useOne() { auto o = std::make_shared<DB::One>(); Widget & r = *new Widget(1); return 0; }
int usePlainType(const Widget & w) { return 0; }
}
"""


def _occ(rng, symbol, roles=0):
    return NS(range=list(rng), symbol=symbol, symbol_roles=roles, enclosing_range=[])


def _at(ln: int, token: str, nth: int = 0):
    """SCIP range of the nth occurrence of `token` on 0-based line `ln` of use.cpp."""
    line = _USE_CPP.splitlines()[ln]
    col = -1
    for _ in range(nth + 1):
        col = line.index(token, col + 1)
    return [ln, col, col + len(token)]


def _docs():
    header = NS(
        relative_path="widget.h",
        symbols=[],
        occurrences=[
            _occ([6, 6, 12], _W, 1),
            _occ([7, 4, 10], _W0),
            _occ([8, 13, 19], _W1),
            _occ([9, 4, 10], _W2),  # declarations
            _occ([12, 6, 9], _ONE, 1),
            _occ([13, 4, 7], _ONE0),
        ],
    )
    use = NS(
        relative_path="use.cpp",
        symbols=[],
        occurrences=[
            _occ(_at(2, "useDirect"), "cxx . . $ DB/useDirect(b126dc7c1de90089).", 1),
            _occ(_at(2, "Widget"), _W),
            _occ(_at(2, "w"), _W1),  # Widget w(4): type ref + ctor ref
            _occ(_at(3, "useMakeShared"), "cxx . . $ DB/useMakeShared(b126dc7c1de90089).", 1),
            _occ(_at(3, "make_shared"), _MS),
            _occ(_at(3, "Widget"), _W),  # make_shared<Widget>: type ref only
            _occ(_at(4, "useOne"), "cxx . . $ DB/useOne(b126dc7c1de90089).", 1),
            _occ(_at(4, "make_shared"), _MS),
            _occ(_at(4, "One", 1), _ONE),  # make_shared<DB::One>()
            _occ(_at(4, "Widget"), _W),  # `Widget & r`: a reference type, not a construction
            _occ(_at(4, "Widget", 1), _W),
            _occ(_at(4, "Widget", 1), _W1),  # new Widget(1)
            _occ(_at(5, "usePlainType"), "cxx . . $ DB/usePlainType(b3b224f593d6a2b7).", 1),
            _occ(_at(5, "Widget"), _W),  # a parameter type: not a construction
        ],
    )
    return [header, use]


class _PB:
    class Document:
        def ParseFromString(self, payload):
            raise AssertionError("streaming is patched in this test")


def _convert(tmp_path: Path, *, changed=None, source: bool = True, full: bool = False) -> dict:
    root = tmp_path / "checkout"
    root.mkdir(exist_ok=True)
    (root / "use.cpp").write_text(_USE_CPP)
    (root / "widget.h").write_text("// header\n" * 20)
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
            changed,
            [],
            "c0ffee",
            "",
            source_root=str(root) if source else "",
            full=full,
        )


def _ctor_edges(delta: dict, path: str) -> list[tuple[str, str, str, int, float]]:
    return sorted(
        (
            e["from_qualified_name"],
            e["to_qualified_name"],
            e["to_signature"],
            e["line"],
            float(e.get("confidence", 1.0)),
        )
        for f in delta["files"]
        if f["path"] == path
        for e in f["edges"]
        if "::Widget::Widget" in e["to_qualified_name"] or "::One::One" in e["to_qualified_name"]
    )


def test_converter_emits_factory_constructions_per_overload(tmp_path: Path) -> None:
    delta = _convert(tmp_path)
    got = _ctor_edges(delta, "use.cpp")
    # direct forms: exactly what scip-clang said, unchanged (the type ref next
    # to them is not under a factory, so it adds nothing)
    assert ("DB::useDirect", "DB::Widget::Widget", "(scip:d4f767463ce0a6b3)", 3, 1.0) in got
    assert ("DB::useOne", "DB::Widget::Widget", "(scip:d4f767463ce0a6b3)", 5, 1.0) in got
    # make_shared<Widget>: three overloads known -> three edges at 0.8
    ms = [g for g in got if g[0] == "DB::useMakeShared"]
    assert [g[2] for g in ms] == [
        "(scip:49f6e7a06ebc5aa8)",
        "(scip:d4f767463ce0a6b3)",
        "(scip:e8b05e15c683b320)",
    ] and all(g[4] == 0.8 and g[3] == 4 for g in ms)
    # make_shared<DB::One>(): one constructor -> one exact edge (qualifier before the token)
    assert ("DB::useOne", "DB::One::One", "(scip:49f6e7a06ebc5aa8)", 5, 1.0) in got
    # a parameter type is not a construction
    assert not [g for g in got if g[0] == "DB::usePlainType"]
    # a full index with source claims the feature; the counter says how many
    assert delta["features"] == ["ctor_calls"] and delta["stats"]["factory_ctor_edges"] == 4
    # the 1.0 edges do not carry a confidence key: an older wire shape stays byte-identical
    plain = [
        e for f in delta["files"] for e in f["edges"] if e["from_qualified_name"] == "DB::useDirect"
    ]
    assert plain and all("confidence" not in e for e in plain)


def test_converter_without_source_or_partial_makes_no_claim(tmp_path: Path) -> None:
    no_src = _convert(tmp_path, source=False)
    assert "features" not in no_src and no_src["stats"]["factory_ctor_edges"] == 0
    assert not [g for g in _ctor_edges(no_src, "use.cpp") if g[0] == "DB::useMakeShared"]
    partial = _convert(tmp_path, changed={"use.cpp"})
    assert "features" not in partial  # per-merge: edges yes, namespace claim no
    assert [g for g in _ctor_edges(partial, "use.cpp") if g[0] == "DB::useMakeShared"]
    # the harness's --full run lists every tracked file as changed and says --full
    assert _convert(tmp_path, changed={"use.cpp", "widget.h"}, full=True)["features"] == [
        "ctor_calls"
    ]


def test_factory_regex_shapes() -> None:
    def f(line: str) -> bool:
        col = line.index("Widget")
        return scip_to_delta._factory_type_ref([line], [0, col, col + 6])

    assert f("auto w = std::make_shared<Widget>(8);")
    assert f("auto w = std::make_unique<const DB::Widget>(8);")
    assert f("    return std::allocate_shared<Widget>(alloc);")
    assert f("v.emplace<Widget>(1);")
    assert f("auto p = make_shared<Widget>();")  # unqualified factory
    assert not f("std::vector<Widget> v;")
    assert not f("std::shared_ptr<Widget> p;")
    assert not f("Widget w(4);")
    assert not f("auto w = std::make_shared<Wrap<Widget>>(1);")  # Wrap is constructed, not Widget
    g = scip_to_delta._factory_type_ref
    assert not g(None, [0, 0, 6]) and not g(["x"], [5, 0, 1])


# ---- compiler tier: the apply ---------------------------------------------------------


class _ApplyRepo:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.state: list[tuple[str, str, str]] = []

    async def live_symbols_for_path(self, org, ns, path, tier):
        return []

    async def live_edges_for_path(self, org, ns, path, tier):
        return []

    async def chunk_spans_for_path(self, org, ns, path):
        return []

    async def insert_rows(self, table, rows):
        self.rows.setdefault(table, []).extend(rows)

    async def set_state(self, org, ns, key, value, detail=""):
        self.state.append((key, value, detail))


@pytest.mark.asyncio
async def test_apply_carries_confidence_and_records_the_feature() -> None:
    from server.code.app.index.scip_delta import ScipDelta, apply_delta

    delta = ScipDelta.model_validate(
        {
            "commit": "c0ffee",
            "features": ["ctor_calls"],
            "files": [
                {
                    "path": "src/a.cpp",
                    "symbols": [],
                    "edges": [
                        {
                            "from_qualified_name": "DB::f",
                            "from_signature": "(scip:1)",
                            "to_qualified_name": "DB::W::W",
                            "to_signature": "(scip:2)",
                            "line": 3,
                            "confidence": 0.8,
                        },
                        {
                            "from_qualified_name": "DB::f",
                            "from_signature": "(scip:1)",
                            "to_qualified_name": "DB::W::W",
                            "to_signature": "(scip:3)",
                            "line": 3,
                        },
                    ],
                }
            ],
        }
    )
    repo = _ApplyRepo()
    with patch("server.code.app.index.scip_delta._indexable_prefixes", return_value=()):
        stats = await apply_delta(repo, "o", "n", delta)  # type: ignore[arg-type]
    confs = sorted(float(r["confidence"]) for r in repo.rows["code_edges"])
    assert confs == [0.8, 1.0]
    assert stats.ctor_calls_recorded is True
    assert [(k, v) for k, v, _ in repo.state] == [("scip_ctor_calls", "1")]
    assert json.loads(repo.state[0][2])["commit"] == "c0ffee"


@pytest.mark.asyncio
async def test_apply_without_the_feature_writes_no_state() -> None:
    from server.code.app.index.scip_delta import ScipDelta, apply_delta

    delta = ScipDelta.model_validate({"commit": "c0ffee", "files": []})
    repo = _ApplyRepo()
    with patch("server.code.app.index.scip_delta._indexable_prefixes", return_value=()):
        stats = await apply_delta(repo, "o", "n", delta)  # type: ignore[arg-type]
    assert stats.ctor_calls_recorded is False and repo.state == []


# ---- read side --------------------------------------------------------------------------


def test_classify_names_the_constructor_gap_last() -> None:
    kind, causes = ep.classify(
        unresolved=0,
        ambiguous=0,
        seed_paths=["src/A.cpp"],
        scip_paths={"src/A.cpp"},
        ctor_edges_absent=True,
    )
    assert (kind, causes) == ("lower_bound", [ep.CAUSE_CTOR])
    _, causes = ep.classify(
        unresolved=1,
        ambiguous=0,
        seed_paths=["src/A.cpp"],
        scip_paths=set(),
        ctor_edges_absent=True,
    )
    assert causes == [ep.CAUSE_UNRESOLVED, ep.CAUSE_TREESITTER, ep.CAUSE_CTOR]
    assert ep.CAUSE_CTOR in ep.CAUSES


def _sym(name: str, sid: str, path: str = "src/A.cpp") -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "name": name.split("::")[-1],
        "kind": "function",
        "path": path,
        "start_line": 10,
        "end_line": 20,
        "tier": "scip",
        "signature": "(scip:1)",
        "doc_comment": "",
    }


class _ReadRepo:
    def __init__(
        self, *, state: dict[str, str], scip_paths: set[str], edges=None, tier: str = "scip"
    ) -> None:
        self.symbols = {
            "DB::ConcurrentHashJoin::ConcurrentHashJoin": {
                **_sym("DB::ConcurrentHashJoin::ConcurrentHashJoin", "sC"),
                "tier": tier,
            },
            "DB::A::insert": {**_sym("DB::A::insert", "sA"), "tier": tier},
        }
        self._state = state
        self._scip_paths = scip_paths
        self._edges = edges or []
        self.state_reads = 0

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, min_confidence=0.0
    ):
        return list(self._edges)

    async def call_misses_for_names(self, org, ns, names):
        return {}

    async def scip_covered_paths(self, org, ns, paths):
        return {p for p in paths if p in self._scip_paths}

    async def scip_stale_paths(self, org, ns, paths):
        return {p for p in paths if p in getattr(self, "stale_paths", set())}

    async def all_state(self, org, ns):
        self.state_reads += 1
        return {k: {"value": v, "detail": "", "updated_at": None} for k, v in self._state.items()}

    async def symbols_fuzzy(self, org, ns, name):
        return []


@pytest.fixture
def intel(monkeypatch: pytest.MonkeyPatch):
    import server.code.app.symbol.graph as h

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(verify, "schedule_code_trace", lambda **kw: None)

    async def no_dispatch(*a, **k):
        return []

    monkeypatch.setattr(h, "_dispatch_seeds", no_dispatch)
    clear_all()
    clear_all()
    yield h
    clear_all()


async def test_constructor_with_no_edges_is_a_lower_bound_until_the_index_carries_them(
    intel,
) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    # compiler tier covers the seed file, override edges exist, nothing declined —
    # every other condition for `exact` holds — but the index predates ctor calls
    repo = _ReadRepo(state={}, scip_paths={"src/A.cpp"})
    with (
        patch.object(intel, "CodeRepo", return_value=repo),
        patch.object(dispatch, "_namespace_has_override_edges", return_value=True),
    ):
        out = await intel.handle_code_callers(
            CodeCallersInput(
                org="o", namespace="n", name="DB::ConcurrentHashJoin::ConcurrentHashJoin"
            )
        )
    assert out.edges == [] and out.epistemic == "lower_bound"
    assert out.causes == [ep.CAUSE_CTOR]
    assert "constructor" in out.note and "make_shared" in out.note
    # the probe is one state read, cached across calls
    with (
        patch.object(intel, "CodeRepo", return_value=repo),
        patch.object(dispatch, "_namespace_has_override_edges", return_value=True),
    ):
        await intel.handle_code_callees(
            CodeCallersInput(
                org="o", namespace="n", name="DB::ConcurrentHashJoin::ConcurrentHashJoin"
            )
        )
    assert repo.state_reads == 1


async def test_constructor_goes_back_to_exact_once_the_compiler_tier_carries_ctor_calls(
    intel,
) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    repo = _ReadRepo(state={"scip_ctor_calls": "1"}, scip_paths={"src/A.cpp"})
    with (
        patch.object(intel, "CodeRepo", return_value=repo),
        patch.object(dispatch, "_namespace_has_override_edges", return_value=True),
    ):
        out = await intel.handle_code_callers(
            CodeCallersInput(
                org="o", namespace="n", name="DB::ConcurrentHashJoin::ConcurrentHashJoin"
            )
        )
    assert out.epistemic == "exact" and out.causes == []
    assert "constructor" not in out.note


async def test_parser_only_seed_needs_the_edge_stage_at_the_running_stamp(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.domain.extract.extractor_stamp import stage_stamps

    stale = _ReadRepo(
        state={"extractor_stamp_edge": "0000000000000000"}, scip_paths=set(), tier="treesitter"
    )
    with (
        patch.object(intel, "CodeRepo", return_value=stale),
        patch.object(dispatch, "_namespace_has_override_edges", return_value=True),
    ):
        out = await intel.handle_code_callers(
            CodeCallersInput(
                org="o", namespace="n", name="DB::ConcurrentHashJoin::ConcurrentHashJoin"
            )
        )
    assert out.causes == [ep.CAUSE_TREESITTER, ep.CAUSE_CTOR]
    clear_all()
    current = _ReadRepo(
        state={"extractor_stamp_edge": stage_stamps()["edge"]}, scip_paths=set(), tier="treesitter"
    )
    with (
        patch.object(intel, "CodeRepo", return_value=current),
        patch.object(dispatch, "_namespace_has_override_edges", return_value=True),
    ):
        out = await intel.handle_code_callers(
            CodeCallersInput(
                org="o", namespace="n", name="DB::ConcurrentHashJoin::ConcurrentHashJoin"
            )
        )
    assert out.causes == [ep.CAUSE_TREESITTER]  # the parser tier carries the edges


async def test_non_constructor_seeds_never_get_the_cause(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    repo = _ReadRepo(state={}, scip_paths={"src/A.cpp"})
    with (
        patch.object(intel, "CodeRepo", return_value=repo),
        patch.object(dispatch, "_namespace_has_override_edges", return_value=True),
    ):
        out = await intel.handle_code_callers(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert")
        )
    assert out.epistemic == "exact" and out.causes == [] and repo.state_reads == 0


async def test_verify_unused_on_a_constructor_says_why_it_cannot_tell(intel) -> None:
    from server.code.api.models_verify import CodeVerifyInput

    repo = _ReadRepo(state={}, scip_paths={"src/A.cpp"})
    with patch.object(verify, "CodeRepo", return_value=repo):
        out = await verify.handle_code_verify(
            CodeVerifyInput(
                org="o",
                namespace="n",
                kind="unused",
                subject="DB::ConcurrentHashJoin::ConcurrentHashJoin",
            )
        )
    assert out.verdict == "not_established" and out.reason == "ctor_edges_absent"
    assert out.floors["epistemic"] == "lower_bound" and out.floors["causes"] == [
        "ctor_edges_absent"
    ]
    assert "edges_are_name_matched" in out.floors  # the standing floors still ride along
    # the ordinary function keeps the ordinary answer
    with patch.object(verify, "CodeRepo", return_value=repo):
        out = await verify.handle_code_verify(
            CodeVerifyInput(org="o", namespace="n", kind="unused", subject="DB::A::insert")
        )
    assert out.verdict == "not_established" and out.reason == "no_caller_edges"
