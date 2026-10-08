"""code.examples: call sites of a resolved symbol, each with the smallest live
chunk around the call; an unknown symbol is not found, and a symbol nobody
calls is an abstaining empty answer rather than an error."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.api.models_navigation import CodeExamplesInput
from server.code.app.symbol import examples as h
from server.code.store.index import CodeRepo
from server.core.errors import NotFoundError, PreconditionFailedError
from server.settings import settings

SEED = {"symbol_id": "s-parse", "qualified_name": "DB::parseQuery", "tier": "scip"}


def _edge(path: str, line: int, caller: str = "DB::caller") -> dict[str, Any]:
    return {"path": path, "line": line, "from_qualified_name": caller, "tier": "scip"}


class _CH:
    def __init__(self, bodies: dict[tuple[str, int], str]) -> None:
        self.bodies = bodies
        self.statements: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.statements.append(" ".join(sql.split()))
        body = self.bodies.get((params["path"], params["line"]))
        return [{"content": body}] if body is not None else []


class _Repo:
    """Answers only the reads code.examples makes; anything else is an AttributeError."""

    def __init__(
        self,
        *,
        seeds: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        tested: list[str] = (),  # type: ignore[assignment]
        bodies: dict[tuple[str, int], str] | None = None,
    ) -> None:
        self.seeds, self.edges, self.tested = seeds, edges, list(tested)
        self._store = CodeRepo(_CH(bodies or {}))  # type: ignore[arg-type]
        self.edge_reads: list[list[str]] = []

    async def innermost_chunk_at(self, org: str, ns: str, path: str, line: int) -> str | None:
        return await self._store.innermost_chunk_at(org, ns, path, line)

    async def symbols_by_ids(self, org: str, ns: str, ids: list[str]) -> list[dict[str, Any]]:
        return [s for s in self.seeds if s["symbol_id"] in ids]

    async def symbols_by_name(self, org: str, ns: str, name: str) -> list[dict[str, Any]]:
        return [s for s in self.seeds if s["qualified_name"] == name]

    async def edges_for_symbols(
        self, org: str, ns: str, ids: list[str], *, direction: str, limit: int
    ) -> list[dict[str, Any]]:
        assert direction == "callers"
        self.edge_reads.append(list(ids))
        return self.edges

    async def test_map_for(self, org: str, ns: str, paths: list[str]) -> list[dict[str, Any]]:
        return [{"src_path": p} for p in paths if p in self.tested]


@pytest.fixture
def traces(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "code-x")
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: recorded.append(kw))
    return recorded


def _run(monkeypatch: pytest.MonkeyPatch, repo: _Repo, **kw: Any) -> Any:
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    return asyncio.run(h.handle_code_examples(CodeExamplesInput(**kw)))


def test_each_site_carries_the_chunk_around_it_and_tested_callers_come_first(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    repo = _Repo(
        seeds=[SEED],
        edges=[_edge("src/b.cpp", 40), _edge("src/a.cpp", 10), _edge("src/a.cpp", 10)],
        tested=["src/b.cpp"],
        bodies={("src/b.cpp", 40): "void b() { parseQuery(); }"},
    )
    out = _run(monkeypatch, repo, name="DB::parseQuery", top_k=3)

    sites = [(e["path"], e["line"], e["test_covered"], e["body"]) for e in out.examples]
    assert sites == [
        ("src/b.cpp", 40, True, "void b() { parseQuery(); }"),
        # a site with no live chunk still answers, with an empty body
        ("src/a.cpp", 10, False, ""),
    ]
    assert repo.edge_reads == [["s-parse"]]
    (trace,) = traces
    assert trace["op"] == "code.examples" and trace["abstain"] is False
    assert trace["returned_paths"] == ["src/b.cpp", "src/a.cpp"]
    assert trace["trace_run_id"] == out.trace_run_id != ""


def test_the_site_body_is_the_tightest_live_chunk_and_is_capped(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    repo = _Repo(
        seeds=[SEED], edges=[_edge("src/a.cpp", 10)], bodies={("src/a.cpp", 10): "x" * 9000}
    )
    out = _run(monkeypatch, repo, symbol_id="s-parse")

    assert len(out.examples[0]["body"]) == 4000
    (sql,) = repo._store._ch.statements
    assert "tombstoned_at IS NULL" in sql
    assert "ORDER BY end_line - start_line ASC LIMIT 1" in sql


def test_top_k_bounds_the_sites(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    repo = _Repo(seeds=[SEED], edges=[_edge(f"src/f{i}.cpp", i + 1) for i in range(6)])
    out = _run(monkeypatch, repo, name="DB::parseQuery", top_k=2)
    assert [e["path"] for e in out.examples] == ["src/f0.cpp", "src/f1.cpp"]


def test_a_symbol_nobody_calls_abstains_with_an_empty_answer(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    out = _run(monkeypatch, _Repo(seeds=[SEED], edges=[]), name="DB::parseQuery")
    assert out.examples == []
    (trace,) = traces
    assert trace["abstain"] is True and trace["retrieval_risk"] == "abstain"


def test_an_unknown_symbol_is_not_found_and_records_no_trace(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    from server.code.app.index import symbol_forms

    async def no_tolerant_match(*_a: Any, **_k: Any) -> tuple[list[dict[str, Any]], str]:
        return [], ""

    monkeypatch.setattr(symbol_forms, "resolve_tolerant", no_tolerant_match)
    with pytest.raises(NotFoundError, match="DB::nothing"):
        _run(monkeypatch, _Repo(seeds=[SEED], edges=[]), name="DB::nothing")
    assert traces == []


def test_the_kill_switch_refuses_before_any_read(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", False)
    with pytest.raises(PreconditionFailedError):
        _run(monkeypatch, _Repo(seeds=[], edges=[]), name="DB::parseQuery")


@pytest.mark.parametrize("top_k", [0, 11])
def test_top_k_outside_one_to_ten_is_refused(top_k: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CodeExamplesInput(name="f", top_k=top_k)
