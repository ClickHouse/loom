"""code.grep neighbours: the graph block
in the same call as the hits — anchors from the hits' enclosing functions,
k callers / callees / tests each, off by default, fail-open, matches only."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.settings import settings

CHUNKS = [
    {
        "path": "src/Foo.cpp",
        "scope": "DB::Foo::bar",
        "start_line": 10,
        "end_line": 40,
        "symbol_ids": ["a"],
        "content": "int x;\nthrow MAGIC_ERR;\nint y;",
    },
    {
        "path": "src/Other.cpp",
        "scope": "DB::baz",
        "start_line": 1,
        "end_line": 5,
        "symbol_ids": ["c"],
        "content": "MAGIC_ERR();",
    },
    # a hit in a class body: no enclosing FUNCTION -> not an anchor
    {
        "path": "src/Context.h",
        "scope": "DB::Context",
        "start_line": 800,
        "end_line": 830,
        "symbol_ids": ["t"],
        "content": "int MAGIC_ERR;",
    },
]
FUNCTIONS = [
    {
        "symbol_id": "a",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "void bar()",
        "path": "src/Foo.cpp",
        "start_line": 10,
        "end_line": 40,
        "tier": "treesitter",
        "kind": "function",
    },
    # the compiler tier's row for the same definition: its edges must join the anchor
    {
        "symbol_id": "a2",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "signature": "void bar()",
        "path": "src/Foo.cpp",
        "start_line": 10,
        "end_line": 10,
        "tier": "scip",
        "kind": "function",
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
        "kind": "function",
    },
]
CALLERS = [
    {
        "edge_kind": "calls",
        "from_symbol_id": "p",
        "to_symbol_id": "a2",
        "path": "src/P.cpp",
        "line": 7,
        "tier": "scip",
        "confidence": 1.0,
        "from_qualified_name": "DB::P::run",
        "to_qualified_name": "DB::Foo::bar",
    },
    {
        "edge_kind": "calls",
        "from_symbol_id": "q",
        "to_symbol_id": "a",
        "path": "src/Q.cpp",
        "line": 9,
        "tier": "treesitter",
        "confidence": 0.8,
        "from_qualified_name": "DB::Q::run",
        "to_qualified_name": "DB::Foo::bar",
    },
]
CALLEES = [
    {
        "edge_kind": "calls",
        "from_symbol_id": "a",
        "to_symbol_id": "z",
        "path": "src/Foo.cpp",
        "line": 12,
        "tier": "treesitter",
        "confidence": 1.0,
        "from_qualified_name": "DB::Foo::bar",
        "to_qualified_name": "DB::Z::go",
    },
    # plumbing: a constructor call every function makes — filtered from callees
    {
        "edge_kind": "calls",
        "from_symbol_id": "a",
        "to_symbol_id": "e",
        "path": "src/Foo.cpp",
        "line": 13,
        "tier": "treesitter",
        "confidence": 1.0,
        "from_qualified_name": "DB::Foo::bar",
        "to_qualified_name": "DB::Exception::Exception",
    },
    # external: never shown
    {
        "edge_kind": "calls",
        "from_symbol_id": "c",
        "to_symbol_id": "s",
        "path": "src/Other.cpp",
        "line": 2,
        "tier": "scip",
        "confidence": 1.0,
        "from_qualified_name": "DB::baz",
        "to_qualified_name": "std::vector::push_back",
    },
]
TESTS = [
    {
        "src_path": "src/Foo.cpp",
        "test_path": "tests/queries/0_stateless/0001_foo.sql",
        "confidence": 0.9,
    },
]
# A document section citing the anchor (the Markdown lane's
# `reads` edge, path = the document) and a SQL statement's table read that
# must NOT count as a document
DOCS = [
    {
        "edge_kind": "reads",
        "from_symbol_id": "d1",
        "to_symbol_id": "a",
        "path": "docs/design.md",
        "line": 40,
        "tier": "treesitter",
        "confidence": 0.7,
        "from_qualified_name": "docs.design::Design::Commit path",
        "to_qualified_name": "DB::Foo::bar",
    },
    {
        "edge_kind": "reads",
        "from_symbol_id": "q1",
        "to_symbol_id": "a",
        "path": "tests/q.sql",
        "line": 3,
        "tier": "treesitter",
        "confidence": 0.9,
        "from_qualified_name": "tests.q::stmt#1",
        "to_qualified_name": "DB::Foo::bar",
    },
]


class _Repo:
    def __init__(self, *, fail_functions: bool = False) -> None:
        self.function_reads: list[list[str]] = []
        self.edge_reads: list[dict[str, Any]] = []
        self.fail_functions = fail_functions

    async def grep_chunks(self, org, ns, pattern, **kw):
        return CHUNKS

    async def symbols_by_ids(self, org, ns, ids):
        return []

    async def all_state(self, org, ns):
        return {}

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        self.function_reads.append(list(paths))
        if self.fail_functions:
            raise RuntimeError("symbols down")
        return [r for r in FUNCTIONS if r["path"] in paths]

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
        self.edge_reads.append(
            {
                "ids": list(ids),
                "direction": direction,
                "kinds": edge_kinds,
                "min_confidence": min_confidence,
                "per_match": per_match,
            }
        )
        if edge_kinds == ["reads"]:
            return [r for r in DOCS if r["to_symbol_id"] in ids]
        rows = CALLERS if direction == "callers" else CALLEES
        col = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        return [r for r in rows if r[col] in ids]

    async def test_map_for(self, org, ns, paths, *, limit=30):
        return [t for t in TESTS if t["src_path"] in paths]

    async def call_misses_for_names(self, org, ns, names):
        return {"baz": {"unresolved": 2}}

    async def scip_covered_paths(self, org, ns, paths):
        return set()

    async def scip_stale_paths(self, org, ns, paths):
        return {p for p in paths if p in getattr(self, "stale_paths", set())}


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def _run(repo: _Repo, **kw) -> Any:
    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep

    async def _empty(*a, **k):
        return {}

    async def go():
        with (
            patch.object(grep, "CodeRepo", return_value=repo),
            patch.object(grep, "index_as_of", new=_empty),
        ):
            return await grep.handle_code_grep(
                CodeGrepInput(org="o", namespace="n", pattern="MAGIC_ERR", **kw)
            )

    return asyncio.run(go())


def test_block_per_anchor_joined_across_tiers_filtered_and_budgeted() -> None:
    repo = _Repo()
    out = _run(repo, neighbours=True, neighbour_k=3)
    assert len(out.matches) == 3, "the hits are what they were"
    # one symbol read over the matched paths, the read code.enclosing makes
    assert repo.function_reads == [["src/Foo.cpp", "src/Other.cpp", "src/Context.h"]]
    # two anchors: the class-body hit resolves to no function
    assert out.anchors == 2 and not out.anchors_capped
    by_name = {e["qualified_name"]: e for e in out.neighbours}
    bar = by_name["DB::Foo::bar"]
    # both tiers' ids were matched: the compiler edge (a2) and the parser edge (a)
    assert bar["callers"] == ["DB::P::run", "DB::Q::run"]
    assert bar["callees"] == ["DB::Z::go"], "constructor plumbing dropped"
    assert bar["tests"] == ["tests/queries/0_stateless/0001_foo.sql"]
    assert bar["docs"] == ["docs.design::Design::Commit path"], "a SQL table read is not a document"
    assert bar["module"] == "src/Foo.cpp".rsplit("/", 1)[0] or bar["module"]
    baz = by_name["DB::baz"]
    assert baz["callees"] == [], "std:: is external"
    assert baz["epistemic"] == "lower_bound" and "unresolved_calls" in baz["causes"]
    assert out.neighbour_tokens > 0
    # the graph block's confidence floor rides along; the documents read
    # takes the Markdown lane's 0.7 citations
    floors = {tuple(r["kinds"]): r["min_confidence"] for r in repo.edge_reads}
    assert floors == {("calls",): 0.8, ("reads",): 0.6}


def test_off_by_default_costs_no_read() -> None:
    repo = _Repo()
    out = _run(repo)
    assert out.matches and out.neighbours == [] and out.anchors == 0 and out.neighbour_tokens == 0
    assert repo.function_reads == [] and repo.edge_reads == []


def test_explicit_false_costs_no_read() -> None:
    repo = _Repo()
    out = _run(repo, neighbours=False)
    assert out.anchors == 0 and repo.function_reads == [] and repo.edge_reads == []


def test_fail_open_keeps_the_hits() -> None:
    repo = _Repo(fail_functions=True)
    out = _run(repo, neighbours=True)
    assert len(out.matches) == 3 and out.neighbours == [] and out.anchors == 0


def test_neighbours_need_matches_mode() -> None:
    from pydantic import ValidationError

    from server.code.api.models_grep import CodeGrepInput

    with pytest.raises(ValidationError, match="mode: 'matches'"):
        CodeGrepInput(org="o", namespace="n", pattern="MAGIC_ERR", mode="files", neighbours=True)
    # unset and False are fine on every mode
    CodeGrepInput(org="o", namespace="n", pattern="MAGIC_ERR", mode="files")
    CodeGrepInput(org="o", namespace="n", pattern="MAGIC_ERR", mode="files", neighbours=False)
