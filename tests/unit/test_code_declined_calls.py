"""Declined call sites: the fast-tier resolver records what
it could not bind, and code.callers / code.impact size their floor with it."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.domain.extract.edges_treesitter import extract_edges, extract_edges_and_misses
from server.code.domain.extract.lang_python import parse_python
from server.settings import settings

_SRC = b"""\
def helper():
    return 1

def caller():
    helper()          # same file: resolves at 1.0
    nowhere()         # no definition anywhere: unresolved
    everywhere()      # several same-named definitions: ambiguous
    print("noise")    # noise name: neither
"""


def _table() -> dict[str, list[tuple[str, str, str]]]:
    return {
        "everywhere": [
            ("idA", "a.mod::everywhere", "function"),
            ("idB", "b.mod::everywhere", "function"),
        ],
    }


def test_extractor_records_misses_by_disposition_and_keeps_edges() -> None:
    pf = parse_python("pkg/m.py", _SRC)
    edges, misses = extract_edges_and_misses(pf, _table())
    calls = [(e.to_qualified_name, e.confidence) for e in edges if e.edge_kind == "calls"]
    assert len(calls) == 1 and calls[0][0].endswith("helper") and calls[0][1] == 1.0
    by = {m.callee_name: m for m in misses}
    assert set(by) == {"nowhere", "everywhere"}  # print is noise, helper resolved
    assert by["nowhere"].disposition == "unresolved" and by["nowhere"].candidates == 0
    assert by["everywhere"].disposition == "ambiguous" and by["everywhere"].candidates == 2
    assert by["everywhere"].from_qualified_name.endswith("caller")
    assert by["everywhere"].path == "pkg/m.py" and by["everywhere"].line == 7
    # the edges-only API is unchanged
    assert [e.to_qualified_name for e in extract_edges(pf, _table())] == [
        e.to_qualified_name for e in edges
    ]


def test_extractor_dedupes_repeated_sites() -> None:
    src = b"def f():\n    g(); g()\n"
    pf = parse_python("x.py", src)
    _, misses = extract_edges_and_misses(pf, {})
    # two calls on one line from one caller collapse to one row
    assert [(m.callee_name, m.line) for m in misses] == [("g", 2)]


# ---- handler: counts ride code.callers and code.impact -------------------------


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
        "signature": "",
        "doc_comment": "",
    }


def _edge(frm: str, to: str, fq: str, tq: str) -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": fq,
        "to_qualified_name": tq,
        "path": "src/X.cpp",
        "line": 5,
        "tier": "scip",
        "edge_kind": "calls",
        "confidence": 1.0,
    }


class _Repo:
    def __init__(self) -> None:
        self.symbols = {"DB::A::insert": _sym("DB::A::insert", "sA")}
        self.callers = {"sA": [_edge("sX", "sA", "DB::X::x", "DB::A::insert")]}
        self.asked: list[list[str]] = []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, min_confidence=0.0
    ):
        if direction != "callers":
            return []
        return [e for sid in ids for e in self.callers.get(sid, [])]

    async def call_misses_for_names(self, org, ns, names):
        self.asked.append(list(names))
        return {"insert": {"unresolved": 5, "ambiguous": 37}}

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return []

    async def test_map_for(self, org, ns, paths, *, limit=30):
        return []

    async def pr_history_for(self, org, ns, *, paths, limit):
        return []

    async def reviewers_by_pr(self, org, ns, pr_numbers):
        return {}

    async def top_reviewers(self, org, ns, *, days=180, limit=6):
        return []


@pytest.fixture
def intel(monkeypatch: pytest.MonkeyPatch):
    from server.code.app.symbol import graph, impact

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")

    async def no_dispatch(*a, **k):
        return []

    for h in (graph, impact):
        monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
        monkeypatch.setattr(h, "_dispatch_seeds", no_dispatch)
    return graph


async def test_callers_sizes_its_floor(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    repo = _Repo()
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_callers(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert")
        )
    assert len(out.edges) == 1
    assert repo.asked == [["insert"]]  # bare name of the seed
    assert out.unresolved_calls == 5 and out.ambiguous_calls == 37 and out.counts_floor is True
    assert "42 call site(s) named like this symbol were declined" in out.note
    assert "37 ambiguous, 5 unresolved" in out.note


async def test_callees_do_not_look_up_declined_calls(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    repo = _Repo()
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_callees(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert")
        )
    assert repo.asked == [] and out.unresolved_calls == 0 and out.ambiguous_calls == 0


async def test_impact_symbol_shape_carries_the_counts(intel) -> None:
    from server.code.api.models_symbol import CodeImpactInput
    from server.code.app.symbol import impact

    repo = _Repo()
    with patch.object(impact, "CodeRepo", return_value=repo):
        out = await impact.handle_code_impact(
            CodeImpactInput(org="o", namespace="n", name="DB::A::insert")
        )
    assert out.unresolved_calls == 5 and out.ambiguous_calls == 37


async def test_lookup_failure_never_fails_the_answer(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    repo = _Repo()

    async def boom(org, ns, names):
        raise RuntimeError("table missing")

    repo.call_misses_for_names = boom  # type: ignore[method-assign]
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_callers(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert")
        )
    assert len(out.edges) == 1 and out.unresolved_calls == 0 and "declined" not in out.note


# ---- rule 3 is one function, shared with closure repair ----------


def test_candidates_for_call_filters_by_written_qualification() -> None:
    from server.code.domain.extract.edges_treesitter import candidates_for_call

    table = {"bar": [("id1", "A::Foo::bar", "function"), ("id2", "B::Baz::bar", "function")]}
    assert candidates_for_call(table, "bar", "bar") == table["bar"]
    assert candidates_for_call(table, "Foo::bar", "bar") == [table["bar"][0]]
    assert candidates_for_call(table, "A::Foo::bar", "bar") == [table["bar"][0]]
    assert candidates_for_call(table, "Nope::bar", "bar") == []
    assert candidates_for_call(table, "zap", "zap") == []


def test_extractor_and_closure_repair_agree_on_the_same_site() -> None:
    """A site the extractor binds at 0.6 is exactly the site closure repair
    binds from its stored row, and a site it declines stays declined."""
    from server.code.app.index.dependents import plan_closure_repair

    src = b"def caller():\n    Foo.bar()\n    baz()\n"
    pf = parse_python("pkg/m.py", src)
    table = {
        "bar": [("id1", "a.mod::Foo::bar", "function"), ("id2", "b.mod::Baz::bar", "function")],
        "baz": [("id3", "c.mod::baz", "function"), ("id4", "d.mod::baz", "function")],
    }
    edges, misses = extract_edges_and_misses(pf, table)
    # a python member call reaches the resolver as its bare attribute (`bar`),
    # carries no `::` qualification, and has two candidates: declined
    assert [e for e in edges if e.edge_kind == "calls"] == []
    assert sorted((m.callee_name, m.call_text, m.disposition) for m in misses) == [
        ("bar", "bar", "ambiguous"),
        ("baz", "baz", "ambiguous"),
    ]

    # the same sites as stored rows, resolved without the parse: declined too
    rows = [
        {"callee_name": m.callee_name, "call_text": m.call_text, "disposition": "unresolved"}
        for m in misses
    ]
    resolvable, targets, left = plan_closure_repair(rows, table, {"bar", "baz"})
    assert resolvable == [] and left == 2
    # a C++ site written `Foo::bar` pins one candidate under the same rule
    rows[0]["call_text"] = "Foo::bar"
    resolvable, targets, left = plan_closure_repair(rows, table, {"bar", "baz"})
    assert [t["symbol_id"] for t in targets] == ["id1"] and left == 1
