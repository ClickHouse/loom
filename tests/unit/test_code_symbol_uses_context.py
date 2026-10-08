"""code.symbol: a single definition brings its uses (a
compact block from the call graph, budgeted before the bodies, skipped for
hubs), and several definitions are ranked by the change at hand
(`near_paths` / `pr_number`), ranking only, `picked_by` disclosed. With the
default off and no context the payload is byte for byte the plain one."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import symbol_context as ctx
from server.code.app import symbol_uses as uses_mod
from server.code.store.index import prefer_scip_edges
from server.settings import settings

ONE = [
    {
        "symbol_id": "f1",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "void bar(int)",
        "path": "src/Foo.cpp",
        "start_line": 10,
        "end_line": 20,
        "tier": "treesitter",
        "kind": "method",
    },
    {
        "symbol_id": "f1s",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "void bar(int)",
        "path": "src/Foo.h",
        "start_line": 5,
        "end_line": 5,
        "tier": "scip",
        "kind": "method",
    },
]

MANY = [
    {
        "symbol_id": "a",
        "qualified_name": "DB::A::listFiles",
        "name": "listFiles",
        "signature": "void listFiles()",
        "path": "src/Disks/A.cpp",
        "start_line": 1,
        "end_line": 9,
        "tier": "scip",
        "kind": "method",
    },
    {
        "symbol_id": "b",
        "qualified_name": "DB::B::listFiles",
        "name": "listFiles",
        "signature": "void listFiles()",
        "path": "src/IO/S3/B.cpp",
        "start_line": 1,
        "end_line": 9,
        "tier": "scip",
        "kind": "method",
    },
    {
        "symbol_id": "c",
        "qualified_name": "DB::C::listFiles",
        "name": "listFiles",
        "signature": "void listFiles()",
        "path": "src/Storages/ObjectStorage/C.cpp",
        "start_line": 1,
        "end_line": 9,
        "tier": "scip",
        "kind": "method",
    },
]

EDGES = [
    {
        "tier": "scip",
        "edge_kind": "calls",
        "to_symbol_id": "f1s",
        "from_symbol_id": "u1",
        "path": "src/Use.cpp",
        "line": 42,
        "from_qualified_name": "DB::use",
        "to_qualified_name": "DB::Foo::bar",
        "confidence": 1.0,
    },
    # the parser tier saw the same call: prefer_scip_edges keeps one row
    {
        "tier": "treesitter",
        "edge_kind": "calls",
        "to_symbol_id": "f1",
        "from_symbol_id": "u1t",
        "path": "src/Use.cpp",
        "line": 42,
        "from_qualified_name": "DB::use",
        "to_qualified_name": "DB::Foo::bar",
        "confidence": 0.8,
    },
    {
        "tier": "treesitter",
        "edge_kind": "calls",
        "to_symbol_id": "f1",
        "from_symbol_id": "u2",
        "path": "src/Other.cpp",
        "line": 7,
        "from_qualified_name": "DB::other",
        "to_qualified_name": "DB::Foo::bar",
        "confidence": 0.6,
    },
    # a test caller ranks after source callers of the same tier
    {
        "tier": "scip",
        "edge_kind": "calls",
        "to_symbol_id": "f1s",
        "from_symbol_id": "t1",
        "path": "src/tests/gtest_foo.cpp",
        "line": 3,
        "from_qualified_name": "TestFoo",
        "to_qualified_name": "DB::Foo::bar",
        "confidence": 1.0,
    },
]

MISSES = [
    # a declined C++ site at 1/2 = 0.5: a candidate above the floor
    {
        "callee_name": "bar",
        "path": "src/Maybe.cpp",
        "line": 12,
        "from_symbol_id": "m1",
        "from_qualified_name": "DB::maybe",
        "call_text": "bar",
        "disposition": "ambiguous",
        "candidates": 2,
    },
    # a Python `.bar(` cannot call a C++ method: dropped by language
    {
        "callee_name": "bar",
        "path": "tests/integration/test_x/test.py",
        "line": 9,
        "from_symbol_id": "m2",
        "from_qualified_name": "test_x",
        "call_text": "bar",
        "disposition": "ambiguous",
        "candidates": 2,
    },
    # written with another scope: dropped
    {
        "callee_name": "bar",
        "path": "src/Scoped.cpp",
        "line": 4,
        "from_symbol_id": "m3",
        "from_qualified_name": "DB::scoped",
        "call_text": "Baz::bar",
        "disposition": "ambiguous",
        "candidates": 2,
    },
    # already an edge at that path:line: not a candidate
    {
        "callee_name": "bar",
        "path": "src/Use.cpp",
        "line": 42,
        "from_symbol_id": "m4",
        "from_qualified_name": "DB::use",
        "call_text": "bar",
        "disposition": "ambiguous",
        "candidates": 2,
    },
    # below the confidence floor (1/20 -> 0.1 floor < 0.2): counted, not listed
    {
        "callee_name": "bar",
        "path": "src/Weak.cpp",
        "line": 1,
        "from_symbol_id": "m5",
        "from_qualified_name": "DB::weak",
        "call_text": "bar",
        "disposition": "ambiguous",
        "candidates": 20,
    },
]


class _FakeRepo:
    def __init__(
        self,
        defs: list[dict[str, Any]],
        *,
        edges=None,
        misses=None,
        stats=None,
        co_change=None,
        prs=None,
    ) -> None:
        self.defs = defs
        self.edges = EDGES if edges is None else edges
        self.misses = MISSES if misses is None else misses
        self.stats = stats or {}
        self.co_change = co_change or {}
        self.prs = prs or {}
        self.calls: list[tuple[str, Any]] = []
        self.kinds: list[tuple[str, Any]] = []
        self.fail: set[str] = set()

    async def symbols_by_name(self, org, ns, name, limit=50):
        return list(self.defs)

    async def symbols_by_ids(self, org, ns, ids):
        return [r for r in self.defs if r["symbol_id"] in ids]

    async def symbols_fuzzy(self, org, ns, needle, *, limit=25):
        return []

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return []

    async def grep_chunks(self, org, ns, pattern, **kw):
        self.calls.append(("grep_chunks", pattern))
        return []

    async def all_state(self, org, ns):
        return {}

    async def edges_for_symbols(self, org, ns, ids, *, direction, limit=200, **kw):
        # The citing-documents read (`reads` edges) is not the
        # call graph the type rule keeps off a type lookup
        op = "edges_for_symbols:reads" if kw.get("edge_kinds") == ["reads"] else "edges_for_symbols"
        self.calls.append((op, (sorted(ids), direction, limit)))
        self.kinds.append(kw.get("edge_kinds"))
        if "edges" in self.fail:
            raise RuntimeError("edges table unavailable")
        return [e for e in self.edges if e["to_symbol_id"] in ids][:limit]

    async def live_call_misses_for_names(self, org, ns, names, *, limit=2000):
        self.calls.append(("live_call_misses_for_names", (list(names), limit)))
        return [m for m in self.misses if m["callee_name"] in names][:limit]

    async def symbol_stats(self, org, ns, ids):
        self.calls.append(("symbol_stats", sorted(ids)))
        return {k: v for k, v in self.stats.items() if k in ids}

    async def co_change_counts(self, org, ns, *, def_paths, near_paths):
        self.calls.append(("co_change_counts", (list(def_paths), list(near_paths))))
        return dict(self.co_change)

    async def prs_by_number(self, org, ns, numbers, **kw):
        self.calls.append(("prs_by_number", list(numbers)))
        return {n: self.prs[n] for n in numbers if n in self.prs}


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    monkeypatch.setattr(settings, "code_symbol_uses_hub_fan_in", 200)
    monkeypatch.setattr(settings, "code_callers_candidate_min_confidence", 0.2)


def _run(repo: _FakeRepo, **fields: Any) -> Any:
    # the mechanism tests ask for the compact block unless they pass `uses`
    if "uses" not in fields:
        fields.setdefault("uses_brief", True)
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import outline
    from server.code.app.symbol import symbol as h

    async def body(r, org, ns, sym):
        return f"<body {sym['symbol_id']}>" + " x" * 40

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=repo),
            patch.object(h, "symbol_body", new=body),
            patch.object(outline, "symbol_body", new=body),
            patch.object(h, "new_trace_run_id", new=lambda: "trace-fixed"),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_symbol(
                CodeSymbolInput(
                    include_history=False,
                    include_examples=False,
                    include_lessons=False,
                    **fields,
                )
            )

    return asyncio.run(go())


def _dump(out: Any) -> str:
    return json.dumps(out.model_dump(mode="json"), sort_keys=True)


def _ops(repo: _FakeRepo) -> list[str]:
    return [c[0] for c in repo.calls]


# ---------------------------------------------------------------- R1: uses ride the definition


def test_one_definition_carries_the_compact_block():
    repo = _FakeRepo(ONE)
    out = _run(repo, name="DB::Foo::bar")
    u = out.uses
    assert isinstance(u, dict)
    assert set(u) == {"count", "files", "top", "truncated", "epistemic"}
    # one scip edge (its parser twin collapsed), one parser edge, one test
    # caller, one candidate above the floor
    assert u["count"] == 4 and u["files"] == 4 and u["truncated"] is False
    tiers = [(s["tier"], s["path"]) for s in u["top"]]
    assert tiers == [
        ("scip", "src/Use.cpp"),
        ("scip", "src/tests/gtest_foo.cpp"),
        ("treesitter", "src/Other.cpp"),
        ("candidate", "src/Maybe.cpp"),
    ]
    assert u["top"][0] == {
        "path": "src/Use.cpp",
        "line": 42,
        "caller": "DB::use",
        "tier": "scip",
        "confidence": 1.0,
    }
    assert u["top"][3]["confidence"] == 0.5
    # a declined site below the floor exists: the answer is a lower bound
    assert u["epistemic"] == "lower_bound"
    assert out.uses_tokens > 0 and out.tokens_returned >= out.uses_tokens
    assert out.uses_truncated is False
    assert "grep_chunks" not in _ops(repo), "the compact block never greps"
    assert _ops(repo).count("symbol_stats") == 1, "fan-in read once, shared with the field"
    assert "edges_for_symbols" in _ops(repo) and "live_call_misses_for_names" in _ops(repo)


def test_no_block_unless_uses_brief_asks_for_it():
    repo = _FakeRepo(ONE)
    plain = _run(repo, name="DB::Foo::bar", uses_brief=False)
    assert plain.uses == [] and "edges_for_symbols" not in _ops(repo)
    asked = _run(_FakeRepo(ONE), name="DB::Foo::bar", uses_brief=True)
    assert isinstance(asked.uses, dict) and asked.uses["count"] == 4 and asked.uses_tokens > 0
    # the full `uses=true` list takes precedence over the brief flag
    both = _run(_FakeRepo(ONE), name="DB::Foo::bar", uses_brief=True, uses=True)
    assert isinstance(both.uses, list) and both.uses_tokens == 0


def test_exact_only_when_every_seed_file_is_compiler_indexed():
    # The definition's file is parser-only (ONE: Foo.cpp treesitter, Foo.h scip): the
    # compiler tier may bind callers no edge here names, so the count is a floor.
    out = _run(_FakeRepo(ONE, misses=[]), name="DB::Foo::bar")
    assert out.uses["epistemic"] == "lower_bound" and out.uses["count"] == 3
    # The compiler tier's row in the definition's own file proves that file indexed.
    covered = [ONE[0], {**ONE[1], "path": "src/Foo.cpp", "start_line": 10}]
    out = _run(_FakeRepo(covered, misses=[]), name="DB::Foo::bar")
    assert out.uses["epistemic"] == "exact" and out.uses["count"] == 3


def test_a_hub_floor_counts_a_site_both_tiers_saw_once():
    from server.code.app.symbol_uses import _count_floor

    edge = {
        "from_qualified_name": "DB::A::run",
        "to_qualified_name": "DB::Foo::bar",
        "path": "src/A.cpp",
        "line": 7,
    }
    edges = [{**edge, "tier": "scip"}, {**edge, "tier": "treesitter"}]
    foreign = {"path": "x.py", "line": 1, "call_text": "bar()", "disposition": "unresolved"}
    assert _count_floor(edges, [foreign], ONE) == 1


def test_several_definitions_carry_no_block():
    repo = _FakeRepo(MANY)
    out = _run(repo, name="listFiles")
    assert out.uses == [] and out.uses_tokens == 0
    assert "edges_for_symbols" not in _ops(repo) and "live_call_misses_for_names" not in _ops(repo)
    assert "uses_tokens" not in out.model_dump(mode="json")


def test_overloads_are_several():
    two = [dict(ONE[1]), {**ONE[1], "symbol_id": "f2s", "signature": "void bar(double)"}]
    assert uses_mod.single_declaration(two) is False
    assert uses_mod.single_declaration(ONE) is True
    assert uses_mod.single_declaration(ONE[:1]) is True


def test_uses_false_skips_the_block_and_matches_the_unasked_payload():
    on = _run(_FakeRepo(ONE), name="DB::Foo::bar", uses=False)
    off = _run(_FakeRepo(ONE), name="DB::Foo::bar", uses_brief=False)
    assert on.uses == [] and _dump(on) == _dump(off)
    dump = off.model_dump(mode="json")
    assert "uses_tokens" not in dump and "picked_by" not in dump
    assert dump["uses"] == [] and dump["uses_truncated"] is False


def test_uses_true_is_the_full_list():
    repo = _FakeRepo(ONE)
    out = _run(repo, name="DB::Foo::bar", uses=True)
    assert isinstance(out.uses, list)
    assert "grep_chunks" in _ops(repo) and "edges_for_symbols" not in _ops(repo)
    assert out.uses_tokens == 0


def test_hub_by_fan_in_stats_skips_the_reads():
    stats = {
        "f1s": {
            "symbol_id": "f1s",
            "qualified_name": "DB::Foo::bar",
            "fan_in_calls": 14380,
            "fan_in_nontest": 14380,
            "fan_in_name": 14380,
            "path": "src/Foo.h",
        }
    }
    repo = _FakeRepo(ONE, stats=stats)
    out = _run(repo, name="DB::Foo::bar")
    assert out.uses == {"count_floor": 14380, "skipped": "hub", "epistemic": "lower_bound"}
    assert "edges_for_symbols" not in _ops(repo) and "live_call_misses_for_names" not in _ops(repo)
    assert {d.fan_in for d in out.definitions} == {0, 14380}, "the one stats read serves the field"
    assert _ops(repo).count("symbol_stats") == 1


def test_hub_by_capped_read(monkeypatch):
    monkeypatch.setattr(uses_mod, "READ_CAP", 3)
    repo = _FakeRepo(ONE)
    out = _run(repo, name="DB::Foo::bar")
    assert out.uses["skipped"] == "hub" and out.uses["count_floor"] >= 3
    assert any(c == ("edges_for_symbols", (["f1", "f1s"], "callers", 3)) for c in repo.calls)


def test_hub_threshold_zero_never_skips_by_stats(monkeypatch):
    monkeypatch.setattr(settings, "code_symbol_uses_hub_fan_in", 0)
    stats = {
        "f1s": {
            "symbol_id": "f1s",
            "qualified_name": "DB::Foo::bar",
            "fan_in_calls": 9999,
            "fan_in_nontest": 9999,
            "fan_in_name": 9999,
            "path": "src/Foo.h",
        }
    }
    out = _run(_FakeRepo(ONE, stats=stats), name="DB::Foo::bar")
    assert "count" in out.uses


def test_block_is_budgeted_before_the_bodies():
    # the bodies are trimmed to what the block leaves of token_budget
    from server.code.domain import units as code_units

    budgets: list[int] = []
    real = code_units.budget_units

    def spy(units, token_budget, **kw):
        budgets.append(token_budget)
        return real(units, token_budget, **kw)

    with patch.object(code_units, "budget_units", new=spy):
        out = _run(_FakeRepo(ONE), name="DB::Foo::bar", token_budget=1000)
        off = _run(_FakeRepo(ONE), name="DB::Foo::bar", token_budget=1000, uses=False)
    assert isinstance(out.uses, dict) and out.uses["count"] == 4
    assert out.uses_tokens > 0
    assert budgets == [1000 - out.uses_tokens, 1000]
    assert out.tokens_returned <= 1000
    # the block's cost is the serialized JSON at the default rate
    assert out.uses_tokens == len(json.dumps(out.uses, separators=(",", ":"))) // 4
    assert off.tokens_returned <= 1000


def test_top_spends_at_most_two_sites_on_one_file():
    one_file = [
        {**EDGES[0], "from_symbol_id": f"u{i}", "line": 100 + i, "from_qualified_name": f"DB::u{i}"}
        for i in range(6)
    ]
    other = [
        {
            **EDGES[0],
            "from_symbol_id": f"o{i}",
            "path": f"src/O{i}.cpp",
            "line": 1,
            "from_qualified_name": f"DB::o{i}",
        }
        for i in range(3)
    ]
    out = _run(_FakeRepo(ONE, edges=one_file + other, misses=[]), name="DB::Foo::bar")
    paths = [s["path"] for s in out.uses["top"]]
    assert paths.count("src/Use.cpp") == 2 and len(paths) == 5
    assert out.uses["count"] == 9 and out.uses["files"] == 4
    assert out.uses["truncated"] is True, "count says what top left out"
    sites = [{"path": "a", "line": i} for i in range(10)]
    assert len(uses_mod.top_sites(sites)) == uses_mod.PER_FILE


def test_top_is_capped_and_truncated_is_said(monkeypatch):
    many = [
        {
            **EDGES[0],
            "from_symbol_id": f"u{i}",
            "path": f"src/U{i}.cpp",
            "line": i,
            "from_qualified_name": f"DB::u{i}",
        }
        for i in range(12)
    ]
    out = _run(_FakeRepo(ONE, edges=many, misses=[]), name="DB::Foo::bar")
    assert out.uses["count"] == 12 and len(out.uses["top"]) == uses_mod.TOP_N
    assert out.uses["truncated"] is True and out.uses["files"] == 12


def test_a_failed_read_never_fails_the_definition():
    repo = _FakeRepo(ONE)
    repo.fail.add("edges")
    out = _run(repo, name="DB::Foo::bar")
    assert out.definitions and out.uses["count"] == 0
    assert out.uses["epistemic"] == "lower_bound" and "note" in out.uses


TYPE_ROW = [
    {
        "symbol_id": "t1",
        "qualified_name": "DB::ChunkBuffer",
        "name": "ChunkBuffer",
        "signature": "class ChunkBuffer",
        "path": "src/Processors/ChunkBuffer.h",
        "start_line": 5,
        "end_line": 40,
        "tier": "scip",
        "kind": "class",
    },
]

TYPE_CHUNKS = [
    {
        "path": "src/Processors/ChunkBuffer.h",
        "scope": "DB::ChunkBuffer",
        "symbol_ids": ["t1"],
        "start_line": 5,
        "end_line": 40,
        "content": "class ChunkBuffer\n{\n    ChunkBuffer();\n};",
    },
    {
        "path": "src/Processors/Use.cpp",
        "scope": "DB::use",
        "symbol_ids": ["u1"],
        "start_line": 10,
        "end_line": 13,
        "content": "void use()\n{\n    ChunkBuffer buf;\n    // ChunkBuffer holds\n}",
    },
    {
        "path": "src/Processors/Other.cpp",
        "scope": "",
        "symbol_ids": [],
        "start_line": 1,
        "end_line": 2,
        "content": '#include "ChunkBuffer.h"\nint z;',
    },
]


class _TypeRepo(_FakeRepo):
    async def grep_chunks(self, org, ns, pattern, **kw):
        self.calls.append(("grep_chunks", pattern))
        self.scope = kw.get("scope")
        return [c for c in TYPE_CHUNKS if pattern in c["content"]]


def test_a_type_brings_its_mentions_not_the_call_graph():
    repo = _TypeRepo(TYPE_ROW, edges=[], misses=[])
    out = _run(repo, name="ChunkBuffer")
    u = out.uses
    assert u["epistemic"] == "lower_bound"
    # the definition's own span is dropped; the code line ranks before the
    # comment and the include
    assert [(s["path"], s["line"], s["tier"], s["confidence"]) for s in u["top"]] == [
        ("src/Processors/Use.cpp", 12, "mention", 1.0),
        ("src/Processors/Other.cpp", 1, "mention", 0.3),
        ("src/Processors/Use.cpp", 13, "mention", 0.3),
    ]
    assert u["top"][0]["caller"] == "DB::use"
    assert u["count"] == 3 and u["files"] == 2 and u["truncated"] is False
    assert "grep_chunks" in _ops(repo)
    # A type's hierarchy (inherits edges) rides along; its call
    # graph still does not
    assert ["inherits"] in repo.kinds and all(k in (["inherits"], ["reads"]) for k in repo.kinds)
    assert uses_mod.is_type(TYPE_ROW) and not uses_mod.is_type(ONE)
    # the read is scoped to the definition's language, noise paths excluded
    assert repo.scope.langs == ("cpp",) and "ci/**" in repo.scope.exclude_globs


def test_route_names_the_block():
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import outline
    from server.code.app.symbol import symbol as h

    seen: list[dict[str, Any]] = []

    async def body(r, org, ns, sym):
        return ""

    async def go(**fields):
        with (
            patch.object(h, "CodeRepo", return_value=_FakeRepo(ONE)),
            patch.object(h, "symbol_body", new=body),
            patch.object(outline, "symbol_body", new=body),
            patch.object(h, "schedule_code_trace", new=lambda **k: seen.append(k)),
        ):
            await h.handle_code_symbol(
                CodeSymbolInput(
                    name="DB::Foo::bar",
                    include_history=False,
                    include_examples=False,
                    include_lessons=False,
                    **fields,
                )
            )

    asyncio.run(go(uses_brief=True))
    asyncio.run(go(uses=True))
    asyncio.run(go(uses=False))
    assert [k["route"] for k in seen] == [
        "symbol_exact+uses_brief",
        "symbol_exact+uses",
        "symbol_exact",
    ]


# ---------------------------------------------------------------- R2: disambiguation by context


def test_no_context_is_byte_identical():
    a = _run(_FakeRepo(MANY), name="listFiles")
    b = _run(_FakeRepo(MANY), name="listFiles", near_paths=[])
    assert _dump(a) == _dump(b)
    assert "picked_by" not in a.model_dump(mode="json")
    assert [d.path for d in a.definitions] == [r["path"] for r in MANY]


def test_same_file_ranks_first_and_keeps_every_definition():
    repo = _FakeRepo(MANY)
    out = _run(repo, name="listFiles", near_paths=["src/IO/S3/B.cpp", "src/IO/S3/B.h"])
    assert [d.path for d in out.definitions] == [
        "src/IO/S3/B.cpp",
        "src/Disks/A.cpp",
        "src/Storages/ObjectStorage/C.cpp",
    ]
    assert out.picked_by == "same_file"
    assert "co_change_counts" not in _ops(repo), "decided before history: no read"
    assert len(out.definitions) == 3


def test_same_dir_ranks_first():
    out = _run(_FakeRepo(MANY), name="listFiles", near_paths=["./src/Storages/ObjectStorage/Z.cpp"])
    assert out.definitions[0].path == "src/Storages/ObjectStorage/C.cpp"
    assert out.picked_by == "same_dir"


def test_co_change_breaks_the_tie_and_is_read_only_then():
    repo = _FakeRepo(MANY, co_change={"src/Storages/ObjectStorage/C.cpp": 7, "src/Disks/A.cpp": 2})
    out = _run(repo, name="listFiles", near_paths=["src/Interpreters/X.cpp"])
    assert out.definitions[0].path == "src/Storages/ObjectStorage/C.cpp"
    assert out.picked_by == "co_change"
    reads = [c for c in repo.calls if c[0] == "co_change_counts"]
    assert len(reads) == 1
    assert reads[0][1] == ([r["path"] for r in MANY], ["src/Interpreters/X.cpp"])


def test_path_distance_when_history_is_silent():
    out = _run(_FakeRepo(MANY), name="listFiles", near_paths=["src/IO/WriteHelpers.cpp"])
    # src/IO/S3/B.cpp shares src/IO: distance 3; the others share only src: 4-5
    assert out.definitions[0].path == "src/IO/S3/B.cpp"
    assert out.picked_by == "path_distance"


def test_none_when_nothing_separates_the_top_two():
    rows = [dict(MANY[0]), {**MANY[0], "symbol_id": "a2", "qualified_name": "DB::A::listFiles2"}]
    out = _run(_FakeRepo(rows), name="listFiles", near_paths=["src/Disks/A.cpp"])
    assert out.picked_by == "none"
    assert [d.unit_id for d in out.definitions] == ["a", "a2"], "ties keep the arrival order"


def test_single_declaration_with_context_says_none_and_keeps_its_order():
    repo = _FakeRepo(ONE)
    out = _run(repo, name="DB::Foo::bar", near_paths=["src/Foo.cpp"])
    assert out.picked_by == "none" and isinstance(out.uses, dict)
    assert [d.unit_id for d in out.definitions] == ["f1", "f1s"]
    assert "co_change_counts" not in _ops(repo)


def test_pr_number_resolves_to_the_ledgers_file_list():
    repo = _FakeRepo(
        MANY, prs={4242: {"pr_number": 4242, "files": ["src/Disks/A.cpp", "docs/x.md"]}}
    )
    out = _run(repo, name="listFiles", pr_number=4242)
    assert out.definitions[0].path == "src/Disks/A.cpp" and out.picked_by == "same_file"
    assert ("prs_by_number", [4242]) in repo.calls


def test_unknown_pr_leaves_the_order_and_says_none():
    repo = _FakeRepo(MANY)
    out = _run(repo, name="listFiles", pr_number=1)
    assert [d.path for d in out.definitions] == [r["path"] for r in MANY]
    assert out.picked_by == "none"
    assert "co_change_counts" not in _ops(repo)


def test_near_paths_capped_at_twenty():
    from pydantic import ValidationError

    from server.code.api.models_symbol import CodeSymbolInput

    with pytest.raises(ValidationError):
        CodeSymbolInput(name="x", near_paths=[f"src/{i}.cpp" for i in range(21)])


def test_path_distance_and_normalisation():
    assert ctx.path_distance("src/a/b.cpp", "src/a/b.cpp") == 0
    assert ctx.path_distance("src/a/b.cpp", "src/a/c.cpp") == 2
    assert ctx.path_distance("src/a/b.cpp", "src/x/y/c.cpp") == 5
    assert ctx.normalize_path("./src/a.cpp") == "src/a.cpp"
    assert ctx.normalize_path("/src/a.cpp") == "src/a.cpp"


def test_rank_is_pure_and_stable():
    ranked, picked = ctx.rank_definitions(MANY, ["src/Disks/A.h"])
    assert [r["symbol_id"] for r in ranked] == ["a", "b", "c"] and picked == "same_dir"
    ranked, picked = ctx.rank_definitions(MANY, [])
    assert [r["symbol_id"] for r in ranked] == ["a", "b", "c"] and picked == "none"


def test_sdk_sends_context_and_flag_only_when_given():
    from sdk.python.loom.code import CodeClient

    sent: list[dict[str, Any]] = []

    class _Transport:
        async def post(self, path, body):
            sent.append(body)
            return {}

    class _Agent:
        transport = _Transport()

    api = CodeClient.__new__(CodeClient)
    api._agent = _Agent()
    asyncio.run(api.symbol("listFiles"))
    asyncio.run(api.symbol("listFiles", near_paths=["src/IO/S3/B.cpp"], pr_number=7, uses=False))
    asyncio.run(api.symbol("listFiles", uses=True, uses_limit=20))
    asyncio.run(api.symbol("listFiles", uses_brief=True))
    assert not ({"uses", "uses_limit", "uses_brief", "near_paths", "pr_number"} & set(sent[0]))
    assert sent[1]["near_paths"] == ["src/IO/S3/B.cpp"] and sent[1]["pr_number"] == 7
    assert sent[1]["uses"] is False and "uses_limit" not in sent[1]
    assert sent[2]["uses"] is True and sent[2]["uses_limit"] == 20
    assert sent[3] == {**sent[0], "uses_brief": True}


def test_prefer_scip_edges_folds_the_tiers_differing_spellings() -> None:
    """Go's parser tier is path-scoped and its compiler tier
    package-scoped — the same call must not surface twice under two names."""
    rows = [
        {
            "tier": "scip",
            "edge_kind": "calls",
            "to_symbol_id": "s1",
            "from_symbol_id": "s2",
            "path": "svc/kms/aws.go",
            "line": 195,
            "from_qualified_name": "example.com/svc/kms::AWSKMS::GenerateEncryptedDEK",
            "to_qualified_name": "example.com/svc/util::AES256Key",
            "confidence": 1.0,
        },
        {
            "tier": "treesitter",
            "edge_kind": "calls",
            "to_symbol_id": "t1",
            "from_symbol_id": "t2",
            "path": "svc/kms/aws.go",
            "line": 195,
            "from_qualified_name": "svc.kms.aws::AWSKMS.GenerateEncryptedDEK",
            "to_qualified_name": "svc.util.util::AES256Key",
            "confidence": 0.6,
        },
        # a different site of the same names stays
        {
            "tier": "treesitter",
            "edge_kind": "calls",
            "to_symbol_id": "t1",
            "from_symbol_id": "t3",
            "path": "svc/kms/gcp.go",
            "line": 150,
            "from_qualified_name": "svc.kms.gcp::GCPKMS.GenerateEncryptedDEK",
            "to_qualified_name": "svc.util.util::AES256Key",
            "confidence": 0.6,
        },
    ]
    kept = prefer_scip_edges(rows)
    assert [(r["tier"], r["path"]) for r in kept] == [
        ("scip", "svc/kms/aws.go"),
        ("treesitter", "svc/kms/gcp.go"),
    ]
