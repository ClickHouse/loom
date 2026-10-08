"""code.verify: three-valued verdicts with the floor rule.

The epistemic contract under test: positive evidence confirms or refutes;
an absence in a floor-graph only ever yields not_established — and an
unresolvable subject REFUSES (with suggestions) rather than answering, so
a typo can never come back as "not established" and be read as doubt about
the real symbol.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from server.core.errors import NotFoundError
from server.settings import settings


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
        "path": "src/A.cpp",
        "line": 5,
        "tier": "scip",
        "edge_kind": "calls",
        "confidence": 1.0,
    }


class _FakeRepo:
    """A → B → C call graph; DB::A::f is the only symbol with callers."""

    def __init__(self) -> None:
        self.symbols = {
            "DB::A::f": _sym("DB::A::f", "sA"),
            "DB::B::g": _sym("DB::B::g", "sB", "src/B.cpp"),
            "DB::C::h": _sym("DB::C::h", "sC", "src/C.cpp"),
        }
        self.callees = {
            "sA": [_edge("sA", "sB", "DB::A::f", "DB::B::g")],
            "sB": [_edge("sB", "sC", "DB::B::g", "DB::C::h")],
        }
        self.callers = {"sA": [_edge("sX", "sA", "DB::X::x", "DB::A::f")]}
        self.files = {"src/A.cpp": {"path": "src/A.cpp", "symbol_count": 3, "index_error": ""}}
        self.chunks: list[dict[str, Any]] = []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def symbols_fuzzy(self, org, ns, needle, *, limit=25):
        return [s for s in self.symbols.values() if needle.lower() in s["name"].lower()]

    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200):
        table = self.callees if direction == "callees" else self.callers
        return [e for sid in ids for e in table.get(sid, [])]

    async def live_symbols_for_path(self, org, ns, path, tier):
        if tier != "scip":
            return []
        return [s for s in self.symbols.values() if s["path"] == path]

    async def live_files_for_path(self, org, ns, path):
        f = self.files.get(path)
        return [f] if f else []

    async def chunk_contains(self, org, ns, path, literal, *, limit=10):
        return [c for c in self.chunks if c["path"] == path and literal in c["content"]]


def _run(kind: str, subject: str, object: str = "", **kw):
    import asyncio

    from server.code.api.models_verify import CodeVerifyInput
    from server.code.app.symbol import verify as h

    async def go():
        with patch.object(h, "CodeRepo", return_value=_FakeRepo()):
            return await h.handle_code_verify(
                CodeVerifyInput(
                    kind=kind, subject=subject, object=object, org="o", namespace="n", **kw
                )
            )

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def test_calls_confirmed_with_chain() -> None:
    out = _run("calls", "DB::A::f", "DB::C::h")
    assert out.verdict == "confirmed"
    assert [e["to"] for e in out.evidence] == ["DB::B::g", "DB::C::h"]


def test_calls_absence_refuted_only_when_compiler_indexed_and_unmentioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refuted contract: no edge in either tier AND the object's bare name
    absent from the subject's definition text AND a compiler-indexed subject.
    Anything short of that stays a floor (not_established), with the reason."""
    from server.code.app.symbol import verify as cs

    async def _body_without_f(repo, org, ns, sym):
        return "void h() { return 42; }"

    async def _complete(repo, org, ns, seeds):
        return True

    monkeypatch.setattr(cs, "symbol_body", _body_without_f)
    monkeypatch.setattr(cs, "_body_complete", _complete)
    out = _run("calls", "DB::C::h", "DB::A::f")  # against the arrow, `f` never mentioned
    assert out.verdict == "refuted" and out.reason == "no_edge_no_mention"
    assert "does not appear" in out.explanation

    async def _body_mentioning_f(repo, org, ns, sym):
        return "void h() { MACRO_THAT_HIDES(f); }"

    monkeypatch.setattr(cs, "symbol_body", _body_mentioning_f)
    out = _run("calls", "DB::C::h", "DB::A::f")
    assert out.verdict == "not_established"
    assert "appears in the subject's text" in out.explanation
    assert "edges_are_name_matched" in out.floors


def test_calls_absence_with_treesitter_only_subject_is_floored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.symbol import verify as cs

    async def _body_without_f(repo, org, ns, sym):
        return "void h() { return 42; }"

    async def _complete(repo, org, ns, seeds):
        return True

    monkeypatch.setattr(cs, "symbol_body", _body_without_f)
    monkeypatch.setattr(cs, "_body_complete", _complete)
    import asyncio

    from server.code.api.models_verify import CodeVerifyInput

    repo = _FakeRepo()
    repo.symbols["DB::C::h"]["tier"] = "treesitter"

    async def go():
        with patch.object(cs, "CodeRepo", return_value=repo):
            return await cs.handle_code_verify(
                CodeVerifyInput(
                    kind="calls", subject="DB::C::h", object="DB::A::f", org="o", namespace="n"
                )
            )

    out = asyncio.run(go())
    assert out.verdict == "not_established" and out.reason == "no_edge_subject_not_compiler_indexed"
    assert "not compiler-indexed" in out.explanation


def test_unused_refuted_by_recorded_caller() -> None:
    out = _run("unused", "DB::A::f")
    assert out.verdict == "refuted"
    assert out.evidence[0]["from"] == "DB::X::x"


def test_unused_is_never_confirmable() -> None:
    out = _run("unused", "DB::C::h")  # zero callers in the graph
    assert out.verdict == "not_established"


def test_unresolvable_subject_refuses_with_suggestions() -> None:
    with pytest.raises(NotFoundError, match="did you mean"):
        _run("unused", "DB::A::F")  # wrong case: unresolvable, fuzzy-close


def test_not_found_carries_did_you_mean() -> None:
    """The refusal's detail carries code.symbol's near misses, capped at five, so the
    claim can be re-checked under the right name."""
    with pytest.raises(NotFoundError) as exc:
        _run("calls", "DB::A::f", "DB::Z::g")  # object misnamed: bare `g` exists
    detail = exc.value.to_detail()
    assert detail["code"] == "RESOURCE_NOT_FOUND" and detail["role"] == "object"
    assert [d["qualified_name"] for d in detail["did_you_mean"]] == ["DB::B::g"]
    assert set(detail["did_you_mean"][0]) == {
        "qualified_name",
        "path",
        "kind",
        "tier",
        "start_line",
        "end_line",
    }
    assert "did you mean: DB::B::g" in detail["message"]
    with pytest.raises(NotFoundError) as none:
        _run("unused", "DB::Nothing::zzz")
    assert none.value.to_detail()["did_you_mean"] == []


def test_not_found_did_you_mean_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_sym(f"DB::N{i}::g", f"s{i}") for i in range(9)]

    async def many(self, org, ns, needle, *, limit=25):
        return rows

    monkeypatch.setattr(_FakeRepo, "symbols_fuzzy", many)
    with pytest.raises(NotFoundError) as exc:
        _run("unused", "DB::Q::g")
    assert len(exc.value.to_detail()["did_you_mean"]) == 5


def test_defines_confirmed_and_refuted_and_floored() -> None:
    assert _run("defines", "src/A.cpp", "DB::A::f").verdict == "confirmed"
    r = _run("defines", "src/A.cpp", "DB::A::nope")
    assert r.verdict == "refuted"  # file indexed clean, symbol table complete
    assert _run("defines", "src/Missing.cpp", "x").verdict == "not_established"


def test_contains_absence_is_floored_not_refuted() -> None:
    out = _run("contains", "src/A.cpp", "MAGIC_LITERAL")
    assert out.verdict == "not_established"
    assert "chunk_coverage" in out.floors


def test_reaches_says_whether_the_walk_ran_out_of_graph_or_of_bounds() -> None:
    """A -> B -> C: reaching a path the graph never touches with depth 5 walks every callee
    (no_path_in_graph, exhausted=False); with depth 1 the walk stops at its bound with B's
    callees unwalked (depth_exhausted, exhausted=True)."""
    whole = _run("reaches", "DB::A::f", "src/Nowhere/", depth=5)
    assert whole.reason == "no_path_in_graph" and whole.floors["exhausted"] is False
    cut = _run("reaches", "DB::A::f", "src/Nowhere/", depth=1)
    assert cut.reason == "depth_exhausted" and cut.floors["exhausted"] is True


async def test_a_hop_read_at_its_limit_is_exhausted() -> None:
    from server.code.app.symbol import verify as h

    class _Full:
        async def edges_for_symbols(self, org, ns, ids, *, limit, **_k):
            return [_edge("sA", f"s{i}", "DB::A::f", f"DB::Z::z{i}") for i in range(limit)]

    chain, _visited, exhausted = await h.bfs_callees(
        _Full(),  # type: ignore[arg-type]
        "o",
        "n",
        ["sA"],
        want="DB::Q::q",
        want_is_path=False,
        depth=1,
    )
    assert chain is None and exhausted is True
