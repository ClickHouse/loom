"""A verdict, not an abstain, for the structural ops.

An empty `code.impact` / `code.outline` / `code.verify` walk names why it
is empty from a closed vocabulary; `abstain` stays true only for the two
verdicts under which the index could not answer (`not_covered`,
`no_match`). The trace row carries the verdict and the
memory.ask grounding hop tags its code-plane reads `consumer='memory.ask'`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.code.domain.index import verdicts as v
from server.settings import settings

# ---- the pure classifier -------------------------------------------------------


def test_vocabulary_is_closed_and_only_two_verdicts_abstain() -> None:
    assert v.VERDICTS == {
        "no_indexed_paths",
        "no_seeds_in_hunks",
        "no_static_callers",
        "not_covered",
        "no_match",
    }
    assert v.ABSTAIN_VERDICTS == {"not_covered", "no_match"}
    assert v.is_abstain("") is False and v.is_abstain("no_static_callers") is False
    assert v.is_abstain("no_match") and v.is_abstain("not_covered")


_ROW = {"path": "src/A.cpp", "symbol_id": "sA", "qualified_name": "DB::A::f", "tier": "scip"}


@pytest.mark.parametrize(
    "kw, expected",
    [
        # rows reached: no verdict, whatever else is true
        ({"impacted": {"sX": {}}, "seeds": [_ROW]}, ""),
        # seeds resolved, nobody calls them
        ({"seeds": [_ROW], "symbol_rows": [_ROW]}, "no_static_callers"),
        # the diff parsed to no path at all
        ({"seed_paths": []}, "no_match"),
        # a source path the index does not hold
        ({"seed_paths": ["src/New.cpp"], "unindexed": {"src/New.cpp"}}, "not_covered"),
        # indexed functions in the file, every hunk outside them
        ({"seed_paths": ["src/A.cpp"], "symbol_rows": [_ROW]}, "no_seeds_in_hunks"),
        # test-only / data-file diff: nothing the graph indexes
        ({"seed_paths": ["tests/queries/0_stateless/1.sql"]}, "no_indexed_paths"),
    ],
)
def test_impact_verdict_precedence(kw: dict[str, Any], expected: str) -> None:
    base: dict[str, Any] = {
        "seed_paths": ["src/A.cpp"],
        "symbol_rows": [],
        "seeds": [],
        "impacted": {},
        "unindexed": set(),
    }
    assert v.impact_verdict(**{**base, **kw}) == expected


def test_outline_verdict_by_file_row_and_language() -> None:
    assert v.outline_verdict(path="src/A.cpp", rows=[{"x": 1}], file_row=None) == ""
    # not held: a source-language path is not covered; a data file is an answer
    assert v.outline_verdict(path="src/A.cpp", rows=[], file_row=None) == "not_covered"
    assert (
        v.outline_verdict(path="tests/q/1.reference", rows=[], file_row=None) == "no_indexed_paths"
    )
    assert v.outline_verdict(path="tests/q/1.sql", rows=[], file_row=None) == "no_indexed_paths"
    # held: a clean file with none of the kinds is an answer; a parse error is not
    held = {"path": "src/A.h", "index_error": "", "symbol_count": 0}
    assert v.outline_verdict(path="src/A.h", rows=[], file_row=held) == "no_indexed_paths"
    broken = {**held, "index_error": "parse failed"}
    assert v.outline_verdict(path="src/A.h", rows=[], file_row=broken) == "no_match"


def test_verify_verdict_only_behind_a_floor() -> None:
    assert v.verify_verdict(kind="calls", claim_verdict="confirmed") == ""
    assert v.verify_verdict(kind="unused", claim_verdict="refuted") == ""
    for kind in ("calls", "reaches", "unused"):
        assert v.verify_verdict(kind=kind, claim_verdict="not_established") == "no_static_callers"
    assert v.verify_verdict(kind="defines", claim_verdict="not_established") == "not_covered"
    assert (
        v.verify_verdict(kind="contains", claim_verdict="not_established", file_row={"path": "p"})
        == "no_match"
    )


# ---- code.impact through the handler -------------------------------------------


def _sym(
    name: str, sid: str, path: str = "src/A.cpp", tier: str = "scip", *, at: int = 10
) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "name": name.split("::")[-1],
        "kind": "function",
        "path": path,
        "start_line": at,
        "end_line": at + 10,
        "tier": tier,
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
    """Symbols in src/A.cpp (compiler tier) and src/T.cpp (parser tier); a
    caller only for DB::A::f. src/A.cpp and src/T.cpp are held files;
    src/New.cpp is not."""

    def __init__(self) -> None:
        self.symbols = {
            "DB::A::f": _sym("DB::A::f", "sA"),
            "DB::A::g": _sym("DB::A::g", "sG", at=30),
            "DB::T::t": _sym("DB::T::t", "sT", path="src/T.cpp", tier="treesitter"),
        }
        self.callers = {"sA": [_edge("sX", "sA", "DB::X::x", "DB::A::f")]}
        self.files = {"src/A.cpp", "src/T.cpp", "tests/queries/0_stateless/1.sql"}

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

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        from server.code.store.index.symbol_rank import _prefer_scip

        # the real read collapses the parser row onto the compiler row
        return _prefer_scip([dict(s) for s in self.symbols.values() if s["path"] in paths])

    async def live_paths_present(self, org, ns, paths):
        return {p for p in paths if p in self.files}

    async def scip_covered_paths(self, org, ns, paths):
        return {p for p in paths if p == "src/A.cpp"}

    async def scip_stale_paths(self, org, ns, paths):
        return {p for p in paths if p in getattr(self, "stale_paths", set())}

    async def test_map_for(self, org, ns, paths):
        return []

    async def pr_history_for(self, org, ns, *, paths, limit):
        return []

    async def reviewers_by_pr(self, org, ns, pr_numbers):
        return {}

    async def top_reviewers(self, org, ns, *, days=180, limit=6):
        return []


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def _impact(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> tuple[Any, list[dict[str, Any]]]:
    from server.code.api.models_symbol import CodeImpactInput
    from server.code.app.symbol import impact as h

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(h, "schedule_code_trace", lambda **k: calls.append(k))

    async def _no_dispatch(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return []

    async def _no_declined(*a: Any, **k: Any) -> tuple[int, int]:
        return 0, 0

    monkeypatch.setattr(h, "_dispatch_seeds", _no_dispatch)
    monkeypatch.setattr(h, "_declined_safe", _no_declined)
    with patch.object(h, "CodeRepo", return_value=_Repo()):
        out = asyncio.run(h.handle_code_impact(CodeImpactInput(**kw)))
    return out, calls


def _diff(path: str, hunk_line: int, header: str = "") -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -{hunk_line},2 +{hunk_line},3 @@ {header}\n+x\n"


def test_impact_rows_reached_carry_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    out, (c,) = _impact(monkeypatch, name="DB::A::f")
    assert out.impacted_symbols and out.verdict == ""
    assert c["abstain"] is False and c["verdict"] == "" and c["retrieval_risk"] == "low"


def test_impact_no_static_callers_is_an_answer_with_the_seeds_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # compiler-tier seed, nobody calls it: low
    out, (c,) = _impact(monkeypatch, name="DB::A::g")
    assert out.impacted_symbols == [] and out.verdict == "no_static_callers"
    assert c["abstain"] is False and c["retrieval_risk"] == "low"
    assert c["verdict"] == "no_static_callers"
    # parser-tier seed: the same answer at medium risk (name-resolved graph)
    out, (c,) = _impact(monkeypatch, name="DB::T::t")
    assert out.verdict == "no_static_callers" and c["retrieval_risk"] == "medium"
    assert c["abstain"] is False
    # diff shape, hunk inside a function nobody calls
    out, (c,) = _impact(monkeypatch, diff=_diff("src/A.cpp", 32, "void g()"))
    assert [t["qualified_name"] for t in out.touched_symbols] == ["DB::A::g"]
    assert out.verdict == "no_static_callers" and c["abstain"] is False


def test_impact_diff_shape_seeds_every_tier_of_a_touched_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A definition the compiler and the parser both index has two ids; a
    caller only the parser saw points at the parser id. The touched list
    carries one row per definition (scip first), the walk seeds both ids."""
    from server.code.api.models_symbol import CodeImpactInput
    from server.code.app.symbol import impact as h

    repo = _Repo()
    # the compiler row is a one-line span inside the parser row's extent
    repo.symbols["DB::A::g"]["end_line"] = 30
    repo.symbols["DB::A::g@ts"] = _sym("DB::A::g", "tG", tier="treesitter", at=30)
    repo.callers["tG"] = [dict(_edge("tR", "tG", "DB::R::run", "DB::A::g"), tier="treesitter")]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(h, "schedule_code_trace", lambda **k: calls.append(k))

    async def _no_declined(*a: Any, **k: Any) -> tuple[int, int]:
        return 0, 0

    monkeypatch.setattr(h, "_declined_safe", _no_declined)
    with patch.object(h, "CodeRepo", return_value=repo):
        out = asyncio.run(h.handle_code_impact(CodeImpactInput(diff=_diff("src/A.cpp", 32))))
    assert [(t["qualified_name"], t["tier"]) for t in out.touched_symbols] == [("DB::A::g", "scip")]
    assert [r["qualified_name"] for r in out.impacted_symbols] == ["DB::R::run"]
    assert out.verdict == "" and calls[0]["abstain"] is False


def test_impact_test_only_diff_is_no_indexed_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    diff = _diff("tests/queries/0_stateless/1.sql", 1) + _diff(
        "tests/queries/0_stateless/1.reference", 1
    )
    out, (c,) = _impact(monkeypatch, diff=diff)
    assert out.touched_symbols == [] and out.verdict == "no_indexed_paths"
    assert c["abstain"] is False and c["retrieval_risk"] == "low"
    assert c["verdict"] == "no_indexed_paths" and c["route"] == "impact_diff"
    # files shape says the same
    out, (c,) = _impact(monkeypatch, files=["tests/performance/x.xml"])
    assert out.verdict == "no_indexed_paths" and c["abstain"] is False


def test_impact_hunks_outside_every_function_is_no_seeds_in_hunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # src/A.cpp has functions at 10-20; the hunk edits line 200 (an include, a constant)
    out, (c,) = _impact(monkeypatch, diff=_diff("src/A.cpp", 200))
    assert out.touched_symbols == [] and out.verdict == "no_seeds_in_hunks"
    assert c["abstain"] is False and c["retrieval_risk"] == "low"


def test_impact_unheld_source_path_abstains_as_not_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, (c,) = _impact(monkeypatch, diff=_diff("src/New.cpp", 3))
    assert out.verdict == "not_covered" and c["abstain"] is True
    assert c["retrieval_risk"] == "abstain" and c["verdict"] == "not_covered"
    assert "external_calls" in out.causes


def test_impact_unparseable_diff_abstains_as_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    out, (c,) = _impact(monkeypatch, diff="this is not a unified diff\njust prose\n")
    assert out.verdict == "no_match" and c["abstain"] is True and c["retrieval_risk"] == "abstain"


# ---- code.outline --------------------------------------------------------------


class _OutlineRepo:
    def __init__(self, rows: list[dict[str, Any]], files: dict[str, dict[str, Any]]) -> None:
        self.rows, self.files = rows, files
        self.file_reads: list[str] = []

    async def outline_for_path(self, org, ns, path, *, kinds=None, limit=3000):
        return [r for r in self.rows if r["path"] == path]

    async def live_files_for_path(self, org, ns, path):
        self.file_reads.append(path)
        f = self.files.get(path)
        return [f] if f else []

    async def all_state(self, org, ns):
        return {}


def _outline(
    monkeypatch: pytest.MonkeyPatch, repo: _OutlineRepo, path: str
) -> tuple[Any, list[dict[str, Any]]]:
    from server.code.api.models_navigation import CodeOutlineInput
    from server.code.app.symbol import outline as h

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(h, "schedule_code_trace", lambda **k: calls.append(k))

    async def _as_of(*a: Any, **k: Any) -> dict[str, Any]:
        return {"commit": "x"}

    monkeypatch.setattr(h, "index_as_of", _as_of)
    with patch.object(h, "CodeRepo", return_value=repo):
        out = asyncio.run(h.handle_code_outline(CodeOutlineInput(path=path)))
    return out, calls


_OUTLINE_ROW = {
    "symbol_id": "f1",
    "qualified_name": "DB::f",
    "name": "f",
    "signature": "int f()",
    "path": "src/A.cpp",
    "start_line": 3,
    "end_line": 9,
    "tier": "scip",
    "kind": "function",
}


def test_outline_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        "src/A.cpp": {"path": "src/A.cpp", "index_error": "", "symbol_count": 1},
        "src/Consts.h": {"path": "src/Consts.h", "index_error": "", "symbol_count": 0},
        "src/Broken.cpp": {
            "path": "src/Broken.cpp",
            "index_error": "parse: EOF",
            "symbol_count": 0,
        },
    }
    repo = _OutlineRepo([_OUTLINE_ROW], files)
    # rows: no verdict, and no file read spent
    out, (c,) = _outline(monkeypatch, repo, "src/A.cpp")
    assert out.entries and out.verdict == "" and c["verdict"] == "" and repo.file_reads == []
    # a data file the index never holds declarations for: an answer
    out, (c,) = _outline(monkeypatch, repo, "tests/queries/0_stateless/1.reference")
    assert out.verdict == "no_indexed_paths" and out.indexed is False
    assert c["abstain"] is False and c["retrieval_risk"] == "low"
    # a held header with nothing of the requested kinds: an answer
    out, (c,) = _outline(monkeypatch, repo, "src/Consts.h")
    assert out.verdict == "no_indexed_paths" and c["abstain"] is False
    # a held file whose parse failed: abstain
    out, (c,) = _outline(monkeypatch, repo, "src/Broken.cpp")
    assert out.verdict == "no_match" and c["abstain"] is True and c["retrieval_risk"] == "abstain"
    # a source file the index does not hold: abstain
    out, (c,) = _outline(monkeypatch, repo, "src/Elsewhere.cpp")
    assert out.verdict == "not_covered" and c["abstain"] is True


def test_outline_file_read_failure_lowers_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _OutlineRepo([], {})

    async def boom(*a: Any, **k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ch down")

    repo.live_files_for_path = boom  # type: ignore[method-assign]
    out, (c,) = _outline(monkeypatch, repo, "src/A.cpp")
    assert out.verdict == "not_covered" and c["abstain"] is True


# ---- code.verify ---------------------------------------------------------------


class _VerifyRepo:
    def __init__(self) -> None:
        self.symbols = {
            "DB::A::f": _sym("DB::A::f", "sA"),
            "DB::B::g": _sym("DB::B::g", "sB", "src/B.cpp"),
        }
        self.files = {"src/A.cpp": {"path": "src/A.cpp", "symbol_count": 1, "index_error": ""}}

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def symbols_fuzzy(self, org, ns, needle, *, limit=25):
        return []

    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200):
        return []

    async def live_symbols_for_path(self, org, ns, path, tier):
        return []

    async def live_files_for_path(self, org, ns, path):
        f = self.files.get(path)
        return [f] if f else []

    async def chunk_contains(self, org, ns, path, literal, *, limit=10):
        return []


def _verify(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> tuple[Any, list[dict[str, Any]]]:
    from server.code.api.models_verify import CodeVerifyInput
    from server.code.app.symbol import verify as h

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(h, "schedule_code_trace", lambda **k: calls.append(k))
    with patch.object(h, "CodeRepo", return_value=_VerifyRepo()):
        out = asyncio.run(h.handle_code_verify(CodeVerifyInput(org="o", namespace="n", **kw)))
    return out, calls


def test_verify_graph_floors_are_answers_and_file_floors_abstain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, (c,) = _verify(monkeypatch, kind="unused", subject="DB::A::f")
    assert out.verdict == "not_established" and out.index_verdict == "no_static_callers"
    assert c["abstain"] is False and c["retrieval_risk"] == "low"
    assert c["verdict"] == "no_static_callers" and c["returned_ids"] == ["not_established"]
    out, (c,) = _verify(monkeypatch, kind="reaches", subject="DB::A::f", object="src/B.cpp")
    assert out.index_verdict == "no_static_callers" and c["abstain"] is False
    # defines on a file the index does not hold: not covered
    out, (c,) = _verify(monkeypatch, kind="defines", subject="src/Missing.cpp", object="x")
    assert out.verdict == "not_established" and out.index_verdict == "not_covered"
    assert c["abstain"] is True and c["retrieval_risk"] == "abstain"
    # contains on a held file whose chunks lack the literal: no match (coverage floor)
    out, (c,) = _verify(monkeypatch, kind="contains", subject="src/A.cpp", object="MAGIC")
    assert out.index_verdict == "no_match" and c["abstain"] is True
    # contains on an unheld file: not covered
    out, (c,) = _verify(monkeypatch, kind="contains", subject="src/Nope.cpp", object="MAGIC")
    assert out.index_verdict == "not_covered" and c["abstain"] is True


# ---- the trace writer ----------------------------------------------------------


class _Insert:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def insert(self, table, rows, *, column_names, settings=None):
        self.rows.append(dict(zip(column_names, rows[0], strict=True)))


def _trace(monkeypatch: pytest.MonkeyPatch, ins: _Insert, **extra: Any) -> None:
    from types import SimpleNamespace

    from server.code.app import common as cc

    monkeypatch.setattr(settings, "code_trace_log_enabled", True)
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: SimpleNamespace(client=ins))

    async def go() -> None:
        cc.schedule_code_trace(
            org="o",
            ns="n",
            op="code.impact",
            query="diff",
            route="impact_diff",
            returned_ids=[],
            returned_paths=[],
            returned_scores=[],
            top_k=200,
            tokens_returned=0,
            abstain=False,
            retrieval_risk="low",
            embedding_model="",
            started_monotonic=0.0,
            trace_run_id="r",
            **extra,
        )
        await asyncio.sleep(0)
        await asyncio.gather(*cc._TRACE_TASKS)

    asyncio.run(go())


def test_trace_row_carries_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    ins = _Insert()
    _trace(monkeypatch, ins, verdict="no_static_callers")
    assert ins.rows[0]["verdict"] == "no_static_callers" and ins.rows[0]["returned_via"] == []


def test_trace_consumer_falls_back_to_the_requests_resolved_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability.consumer import set_current_consumer

    ins = _Insert()
    set_current_consumer("service-account")
    try:
        _trace(monkeypatch, ins)  # caller sent none
        _trace(monkeypatch, ins, consumer="clickgap")  # explicit tag wins
    finally:
        set_current_consumer("")
    assert [r["consumer"] for r in ins.rows] == ["service-account", "clickgap"]
    ins = _Insert()
    _trace(monkeypatch, ins)
    assert ins.rows[0]["consumer"] == ""  # no request in flight: unchanged


# ---- C9: the grounding hop tags its reads --------------------------------------


async def test_grounding_hop_tags_code_search_and_code_symbol_as_memory_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import server.code.app.search.handler as hs
    import server.code.app.symbol as hy
    import server.code.domain.idents as cs
    from server.memory.app import code_grounding as g

    monkeypatch.setattr(cs, "embedded_idents", lambda q: {"FooBar"})
    seen: list[tuple[str, str]] = []

    async def fake_symbol(inp: Any) -> Any:
        seen.append(("symbol", inp.consumer))
        return SimpleNamespace(definitions=[])

    async def fake_search(inp: Any) -> Any:
        seen.append(("search", inp.consumer))
        return SimpleNamespace(abstain=True, results=[])

    monkeypatch.setattr(hy, "handle_code_symbol", fake_symbol)
    monkeypatch.setattr(hs, "handle_code_search", fake_search)
    inp = SimpleNamespace(code_namespace="code-x", code_org="o", org="o")
    st = SimpleNamespace(
        memory_ask_code_namespace="",
        memory_ask_code_org="",
        code_org="o",
        memory_ask_code_hop_symbols=0,
    )
    await g._inject_code_context("what does FooBar do", inp, st)
    assert g.GROUNDING_CONSUMER == "memory.ask"
    assert sorted(seen) == [("search", "memory.ask"), ("symbol", "memory.ask")]


# ---- the catalogue lists the new fields ----------------------------------------


def test_capabilities_catalogue_lists_the_verdict_fields() -> None:
    from server.operations.capabilities import capability_snapshot

    ops = capability_snapshot()["ops"]
    assert "verdict" in ops["code.impact"]["response_fields"]
    assert "verdict" in ops["code.outline"]["response_fields"]
    assert "index_verdict" in ops["code.verify"]["response_fields"]
    # the claim verdict keeps its name (wire rule: fields are added, never renamed)
    assert "verdict" in ops["code.verify"]["response_fields"]
