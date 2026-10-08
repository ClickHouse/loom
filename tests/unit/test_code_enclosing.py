"""code.enclosing: batched path:line -> innermost enclosing function."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.settings import settings

ROWS = [
    {
        "symbol_id": "a",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "void bar(int)",
        "path": "src/Foo.cpp",
        "start_line": 10,
        "end_line": 40,
        "tier": "treesitter",
    },
    # lambda-sized inner span inside bar: must win over bar for line 22
    {
        "symbol_id": "b",
        "qualified_name": "DB::Foo::bar::$lambda",
        "name": "$lambda",
        "signature": "",
        "path": "src/Foo.cpp",
        "start_line": 20,
        "end_line": 25,
        "tier": "treesitter",
    },
    {
        "symbol_id": "c",
        "qualified_name": "DB::baz",
        "name": "baz",
        "signature": "int baz()",
        "path": "src/Other.cpp",
        "start_line": 1,
        "end_line": 5,
        "tier": "scip",
    },
]

# class / struct rows with a body extent (types_for_paths): Context.h's class
# body holds member declarations that no function contains
TYPE_ROWS = [
    {
        "symbol_id": "t",
        "qualified_name": "DB::Context",
        "name": "Context",
        "signature": "",
        "path": "src/Context.h",
        "start_line": 773,
        "end_line": 2186,
        "tier": "scip",
        "kind": "class",
    },
    {
        "symbol_id": "u",
        "qualified_name": "DB::Context::QueryAccessInfo",
        "name": "QueryAccessInfo",
        "signature": "",
        "path": "src/Context.h",
        "start_line": 800,
        "end_line": 830,
        "tier": "treesitter",
        "kind": "struct",
    },
]


class _FakeRepo:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        self.calls.append(list(paths))
        return [r for r in ROWS if r["path"] in paths]

    async def types_for_paths(self, org, ns, paths, *, limit=400):
        self.type_calls = getattr(self, "type_calls", []) + [list(paths)]
        return [r for r in TYPE_ROWS if r["path"] in paths]

    async def all_state(self, org, ns):
        return {}


def _run(locations: list[str]) -> tuple[Any, _FakeRepo]:
    from server.code.api.models_navigation import CodeEnclosingInput
    from server.code.app.symbol import enclosing as h

    fake = _FakeRepo()

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "index_as_of", new=_as_of),
        ):
            return await h.handle_code_enclosing(CodeEnclosingInput(locations=locations))

    return asyncio.run(go()), fake


async def _as_of(*a, **k):
    return {"commit": "x"}


def test_enclosing_batches_paths_and_preserves_order(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    out, fake = _run(
        [
            "src/Other.cpp:3",
            "./src/Foo.cpp:12",
            "src/Foo.cpp:22",
            "src/Foo.cpp:99",
            "src/Missing.cpp:1",
            "garbage",
        ]
    )
    assert fake.calls == [["src/Other.cpp", "src/Foo.cpp", "src/Missing.cpp"]], (
        "one batched read, distinct paths"
    )
    got = [(r["path"], r["line"], r["qualified_name"], r["tier"]) for r in out.results]
    assert got == [
        ("src/Other.cpp", 3, "DB::baz", "scip"),
        ("src/Foo.cpp", 12, "DB::Foo::bar", "treesitter"),
        ("src/Foo.cpp", 22, "DB::Foo::bar::$lambda", "treesitter"),  # innermost wins
        ("src/Foo.cpp", 99, "", ""),  # outside every span -> file scope
        ("src/Missing.cpp", 1, "", ""),  # unindexed path -> soft miss
        ("garbage", 0, "", ""),  # unparseable -> soft miss, batch survives
    ]
    assert out.resolved == 3
    assert out.paths_indexed == 2 and out.paths_total == 3
    assert out.results[0]["symbol"] == "baz" and out.results[0]["signature"] == "int baz()"
    assert out.as_of == {"commit": "x"}


# bar ~180 tokens, the lambda ~4, baz ~60: a 200-token budget takes the first two
BODIES = {
    "a": "void bar(int x) {\n" + "  step(x);\n" * 70 + "}",
    "b": "[&] { tiny(); }",
    "c": "int baz() {\n" + "  return 1;\n" * 20 + "}",
}


def _run_bodies(locations: list[str], budget: int) -> tuple[Any, _FakeRepo, list[str]]:
    from server.code.api.models_navigation import CodeEnclosingInput
    from server.code.app.symbol import enclosing as h

    fake = _FakeRepo()
    fetched: list[str] = []

    async def body(repo, org, ns, sym):
        fetched.append(str(sym["symbol_id"]))
        return BODIES[str(sym["symbol_id"])]

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "index_as_of", new=_as_of),
            patch.object(h, "symbol_body", new=body),
        ):
            return await h.handle_code_enclosing(
                CodeEnclosingInput(locations=locations, include_body=True, token_budget=budget)
            )

    return asyncio.run(go()), fake, fetched


def test_include_body_fetches_once_per_function_and_fills_by_first_appearance(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    # bar twice (lines 12 and 30), the lambda once, baz once, one miss
    out, _fake, fetched = _run_bodies(
        ["src/Foo.cpp:12", "src/Foo.cpp:30", "src/Foo.cpp:22", "src/Other.cpp:3", "src/Foo.cpp:99"],
        budget=200,
    )
    assert fetched == ["a", "b", "c"], "one body read per distinct function, first-appearance order"
    r12, r30, r22, r3, r99 = out.results
    assert r12["body"] == BODIES["a"] and r30["body"] == BODIES["a"], (
        "both hits in bar carry its body"
    )
    assert r12["body_elided"] is False and r12["next"] == ""
    # bar (~180 tokens) fits the 200-token budget; the lambda (4) fits; baz (~60) does not
    assert r22["body"] == BODIES["b"]
    assert r3["body"] == "" and r3["body_elided"] is True
    assert r3["next"] == 'code.symbol(symbol_id="c")', "elided body ships its one-call escalation"
    assert "body" not in r99 and r99["symbol_id"] == "", "unresolved rows are unchanged"
    assert out.bodies_returned == 2 and out.bodies_elided == 1
    assert out.tokens_returned == len(BODIES["a"]) // 4 + len(BODIES["b"]) // 4
    assert r12["symbol_id"] == "a", "symbol_id rides on every resolved row (default mode too)"


def test_default_mode_has_no_body_fields(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    out, _fake = _run(["src/Foo.cpp:12"])
    assert "body" not in out.results[0] and out.bodies_returned == 0 and out.tokens_returned == 0


def test_line_in_a_class_body_falls_back_to_the_type(monkeypatch):
    """A member declaration in a header sits in no function; the class whose
    body holds it is the enclosing scope, with kind saying so. The type read
    happens only for paths that left a line unresolved (Foo.cpp:12 resolves
    to a function, so Foo.cpp is not in the type batch)."""
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    out, fake = _run(
        ["src/Context.h:1265", "src/Context.h:810", "src/Foo.cpp:12", "src/Context.h:5"]
    )
    assert fake.type_calls == [["src/Context.h"]], (
        "type read only for the path with unresolved lines"
    )
    r1265, r810, r12, r5 = out.results
    assert (r1265["qualified_name"], r1265["kind"], r1265["start_line"], r1265["end_line"]) == (
        "DB::Context",
        "class",
        773,
        2186,
    )
    assert (r810["qualified_name"], r810["kind"]) == ("DB::Context::QueryAccessInfo", "struct"), (
        "innermost type wins, as with functions"
    )
    assert (r12["qualified_name"], r12["kind"]) == ("DB::Foo::bar", "function")
    assert r5["symbol"] == "" and r5["kind"] == "", "before the class opens: file scope"
    assert out.resolved == 3


# ---------------------------------------------------------------------------
# neighbours: a graph evidence block per enclosing function
# ---------------------------------------------------------------------------

# bar has a second, compiler-tier id ("a2") in the same rows — edges keyed by
# either id belong to the same anchor
ROWS_TWO_TIER = [
    *ROWS,
    {
        "symbol_id": "a2",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "",
        "path": "src/Foo.cpp",
        "start_line": 10,
        "end_line": 10,
        "tier": "scip",
    },
]


def _edge(frm, to, frm_name, to_name, tier="scip", conf=1.0, line=1):
    return {
        "tier": tier,
        "edge_kind": "calls",
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": frm_name,
        "to_qualified_name": to_name,
        "path": "src/X.cpp",
        "line": line,
        "confidence": conf,
    }


CALLERS = [
    _edge("c1", "a2", "DB::One::run", "DB::Foo::bar"),  # via the scip id
    _edge("c2", "a", "DB::Two::run", "DB::Foo::bar", tier="treesitter", conf=0.8),
    _edge(
        "c2", "a", "DB::Two::run", "DB::Foo::bar", tier="treesitter", conf=0.8, line=2
    ),  # dup name
    _edge("a", "a", "DB::Foo::bar", "DB::Foo::bar"),  # recursion: self dropped
    _edge("c3", "c", "DB::Three::run", "DB::baz"),
]
CALLEES = [
    _edge("a", "x1", "DB::Foo::bar", "std::vector::push_back"),  # external: dropped
    _edge("a", "x0", "DB::Foo::bar", "DB::Exception::Exception"),  # constructor: plumbing, dropped
    _edge("a", "x2", "DB::Foo::bar", "DB::Helper::go"),
    _edge("a", "x3", "DB::Foo::bar", "DB::Helper::stop"),
    _edge("a", "x4", "DB::Foo::bar", "DB::Helper::pause"),
]
TESTS = [
    {
        "src_path": "src/Foo.cpp",
        "test_path": "tests/queries/0_stateless/0001_foo.reference",
        "confidence": 0.95,
    },
    {
        "src_path": "src/Foo.cpp",
        "test_path": "tests/queries/0_stateless/0001_foo.sql",
        "confidence": 0.9,
    },
    {
        "src_path": "src/Foo.cpp",
        "test_path": "tests/queries/0_stateless/0002_foo.sql",
        "confidence": 0.5,
    },
    {
        "src_path": "src/Foo.cpp",
        "test_path": "tests/queries/0_stateless/0001_foo.sql",
        "confidence": 0.4,
    },
    {
        "src_path": "src/Foo.cpp",
        "test_path": "tests/queries/0_stateless/0003_foo.sql",
        "confidence": 0.3,
    },
]


class _GraphRepo(_FakeRepo):
    def __init__(self, *, fail_edges: bool = False) -> None:
        super().__init__()
        self.edge_calls: list[dict[str, Any]] = []
        self.fail_edges = fail_edges

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        self.calls.append(list(paths))
        return [r for r in ROWS_TWO_TIER if r["path"] in paths]

    async def edges_for_symbols(
        self,
        org,
        ns,
        ids,
        *,
        direction,
        edge_kinds=None,
        limit=200,
        min_confidence=0.0,
        per_match=None,
    ):
        self.edge_calls.append(
            {
                "ids": list(ids),
                "direction": direction,
                "kinds": edge_kinds,
                "per_match": per_match,
                "min_confidence": min_confidence,
            }
        )
        if self.fail_edges:
            raise RuntimeError("edges down")
        rows = CALLERS if direction == "callers" else CALLEES
        col = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        return [r for r in rows if r[col] in ids]

    async def test_map_for(self, org, ns, paths, *, limit=30):
        self.test_paths = list(paths)
        return [t for t in TESTS if t["src_path"] in paths]

    async def call_misses_for_names(self, org, ns, names):
        self.miss_names = list(names)
        return {"baz": {"unresolved": 2}}

    async def scip_covered_paths(self, org, ns, paths):
        return set()

    async def scip_stale_paths(self, org, ns, paths):
        return {p for p in paths if p in getattr(self, "stale_paths", set())}


def _run_neighbours(locations, *, neighbours=True, k=2, repo=None):
    from server.code.api.models_navigation import CodeEnclosingInput
    from server.code.app.symbol import enclosing as h

    fake = repo or _GraphRepo()

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "index_as_of", new=_as_of),
        ):
            return await h.handle_code_enclosing(
                CodeEnclosingInput(locations=locations, neighbours=neighbours, neighbour_k=k)
            )

    return asyncio.run(go()), fake


def _code_intel(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def test_neighbours_block_per_anchor_capped_filtered_and_joined_across_tiers(monkeypatch):
    _code_intel(monkeypatch)
    # bar twice, the lambda, baz, a miss, a class-body line (type anchors get no block)
    out, fake = _run_neighbours(
        [
            "src/Foo.cpp:12",
            "src/Foo.cpp:30",
            "src/Foo.cpp:22",
            "src/Other.cpp:3",
            "src/Foo.cpp:99",
            "src/Context.h:1265",
        ],
    )
    assert out.anchors == 3 and not out.anchors_capped
    assert [n["qualified_name"] for n in out.neighbours] == [
        "DB::Foo::bar",
        "DB::Foo::bar::$lambda",
        "DB::baz",
    ], "distinct functions, first appearance"
    bar, lam, baz = out.neighbours
    # both directions read once, per-anchor cap in SQL with one k of headroom,
    # every id the rows carry for the anchor's name (a AND a2) in the match list
    # plus the citing-documents read (reads edges, callers side)
    assert [(c["direction"], c["kinds"]) for c in fake.edge_calls] == [
        ("callers", ["calls"]),
        ("callees", ["calls"]),
        ("callers", ["reads"]),
    ]
    assert [c["per_match"] for c in fake.edge_calls] == [4, 6, 4], (
        "2k callers, 3k callees headroom, 2k docs"
    )
    calls = [c for c in fake.edge_calls if c["kinds"] == ["calls"]]
    assert len(calls) == 2 and all(c["min_confidence"] == 0.8 for c in calls)
    assert {"a", "a2", "b", "c"} <= set(fake.edge_calls[0]["ids"])
    assert bar["callers"] == ["DB::One::run", "DB::Two::run"], (
        "scip-id edge joined to the anchor, duplicate name folded, recursion dropped"
    )
    assert bar["callees"] == ["DB::Helper::go", "DB::Helper::stop"], (
        "external and constructor dropped, cut to k"
    )
    assert bar["tests"] == [
        "tests/queries/0_stateless/0001_foo.sql",
        "tests/queries/0_stateless/0002_foo.sql",
    ], "runnable ahead of its .reference, one per stem, confidence order, cut to k"
    assert bar["module"] == "src" and bar["symbol_id"] == "a" and bar["path"] == "src/Foo.cpp"
    assert lam["callers"] == [] and lam["callees"] == [] and lam["tests"] == bar["tests"]
    # baz: two declined call sites named like it -> a lower bound, with the cause
    assert baz["epistemic"] == "lower_bound" and "unresolved_calls" in baz["causes"]
    assert "treesitter_only_files" in bar["causes"], "no compiler row proves Foo.cpp"
    assert sorted(fake.miss_names) == ["$lambda", "bar", "baz"]
    assert fake.test_paths == ["src/Foo.cpp", "src/Other.cpp"]
    assert out.neighbour_tokens > 0 and out.tokens_returned == out.neighbour_tokens
    assert out.results[0]["symbol_id"] == "a", "rows keep symbol_id: the join key for the block"


def test_neighbours_off_unless_the_request_sets_them(monkeypatch):
    _code_intel(monkeypatch)
    out, fake = _run_neighbours(["src/Foo.cpp:12"], neighbours=None)
    assert out.neighbours == [] and out.anchors == 0 and fake.edge_calls == []
    out, fake = _run_neighbours(["src/Foo.cpp:12"], neighbours=False)
    assert out.anchors == 0 and fake.edge_calls == []


def test_neighbours_fail_open_and_anchor_cap(monkeypatch):
    _code_intel(monkeypatch)
    out, _ = _run_neighbours(
        ["src/Foo.cpp:12", "src/Other.cpp:3"], repo=_GraphRepo(fail_edges=True)
    )
    assert out.resolved == 2 and out.anchors == 2, (
        "a failed edge read costs the block, not the names"
    )
    assert all(n["callers"] == [] and n["callees"] == [] for n in out.neighbours)
    assert out.neighbours[0]["tests"], "the reads that did answer still ship"

    from server.code.app.symbol import neighbours as h

    many = [
        {
            "symbol_id": f"s{i}",
            "qualified_name": f"DB::f{i}",
            "symbol": f"f{i}",
            "path": f"src/M{i % 3}/f{i}.cpp",
            "kind": "function",
        }
        for i in range(25)
    ]

    async def go():
        return await h._attach_enclosing_neighbours(_GraphRepo(), "o", "n", many, [], 5)

    entries, capped, _tokens = asyncio.run(go())
    assert len(entries) == 20 and capped, "at most 20 anchors per call"
    assert entries[0]["module"] == "src/M0"


def test_plumbing_callees_and_runnable_tests():
    from server.code.app.symbol.neighbours import _is_plumbing_callee, _prefer_runnable_tests

    assert _is_plumbing_callee("DB::Exception::Exception")
    assert _is_plumbing_callee("FormatStringHelperImpl::FormatStringHelperImpl")
    assert _is_plumbing_callee("DB::Settings::operator[]") and _is_plumbing_callee("getLogger")
    assert not _is_plumbing_callee("DB::MergeTreeData::checkAlterEligibility")
    assert not _is_plumbing_callee("DB::Foo::operate"), "only operator overloads are plumbing"
    assert _prefer_runnable_tests(
        ["t/01_a.reference", "t/02_b.reference", "t/01_a.sql", "t/03_c.sh"], 2
    ) == ["t/01_a.sql", "t/03_c.sh"]
    assert _prefer_runnable_tests(["t/02_b.reference"], 3) == ["t/02_b.reference"], (
        "only the expectation is mapped: still named"
    )


def test_module_of():
    from server.code.app.symbol import module_of

    assert module_of("src/Interpreters/Foo.cpp") == "src/Interpreters"
    assert module_of("src/Foo.cpp") == "src"
    assert module_of("base/poco/Foundation/src/x.cpp") == "base/poco"
    assert module_of("Foo.cpp") == ""
