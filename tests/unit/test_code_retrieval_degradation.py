"""A failed retrieval leg stays visible after HTTP, MCP and fan-out shaping."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
from server.code.app import search_fanout
from server.code.app.search import handler
from server.code.app.search.state import CodeSearchResult
from server.code.domain.deadline import Budget
from server.code.domain.units import CodeUnit
from server.transport.code_lean import lean_code
from server.transport.lean_results import lean
from tests.unit import test_code_search_deadline as search_fixture


class _UnavailableVector(search_fixture._HandlerRepo):
    async def vector_search_chunks(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private provider response with secret-token")


class _UnavailableLexical(search_fixture._HandlerRepo):
    async def lexical_search_chunks(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private database response with secret-token")


class _NoLexicalMatch(_UnavailableVector):
    async def lexical_search_chunks(self, *args: Any, **kwargs: Any) -> Any:
        return []


@pytest.mark.parametrize(
    ("repo_type", "leg", "unit"),
    [(_UnavailableVector, "vector", "L1"), (_UnavailableLexical, "lexical", "V1")],
)
def test_a_failed_leg_is_visible_to_the_default_mcp_reader(
    monkeypatch: pytest.MonkeyPatch, repo_type: Any, leg: str, unit: str
) -> None:
    data, _ = search_fixture._handle(monkeypatch, repo_type(vector_s=0.0))
    shaped = lean_code("code.search", lean(data))
    assert [row["unit_id"] for row in data["results"]] == [unit]
    assert shaped["partial"] is True
    assert shaped["partial_reason"] == f"retrieval unavailable: {leg}"
    assert leg in shaped["note"] and "health" in shaped["note"]
    assert "secret-token" not in str(shaped) and "private" not in str(shaped)


def test_an_outage_abstention_keeps_its_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    data, _ = search_fixture._handle(monkeypatch, _NoLexicalMatch(vector_s=0.0))
    shaped = lean_code("code.search", lean(data))
    assert shaped["abstain"] is True and shaped["results"] == []
    assert shaped["partial"] is True
    assert shaped["partial_reason"] == "retrieval unavailable: vector"
    assert "health" in shaped["note"]


def test_failed_retrieval_and_a_deadline_are_both_disclosed() -> None:
    budget = Budget(ms=500)
    budget.drop("rerank skipped")
    result = CodeSearchResult(retrieval_work={"vector_leg_failed": 1}, budget=budget)
    assert result.partial is True
    assert result.partial_reason == "deadline 500 ms: rerank skipped; retrieval unavailable: vector"


def test_a_failed_leg_and_a_grace_timeout_are_both_disclosed() -> None:
    result = CodeSearchResult(
        retrieval_work={"vector_leg_failed": 1}, legs_timed_out=["hyde"], epistemic="partial"
    )
    assert result.partial is True
    assert result.partial_reason == "retrieval timed out: hyde; retrieval unavailable: vector"


def test_an_unavailable_embedder_does_not_look_like_healthy_lexical_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> Any:
        raise RuntimeError("private provider initialization with secret-token")

    monkeypatch.setattr(search_fixture, "_Embedder", unavailable)
    data, _ = search_fixture._handle(monkeypatch, search_fixture._HandlerRepo(vector_s=0.0))
    assert data["partial"] is True
    assert data["partial_reason"] == "retrieval unavailable: vector"
    assert "secret-token" not in data["note"]


def test_deliberate_lexical_search_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    data, _ = search_fixture._handle(monkeypatch, _UnavailableVector(vector_s=0.0), legs="lexical")
    assert data["results"]
    assert "partial" not in data and "partial_reason" not in data and not data["note"]


def test_the_warning_clears_when_retrieval_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    failed, _ = search_fixture._handle(monkeypatch, _UnavailableVector(vector_s=0.0))
    recovered, _ = search_fixture._handle(monkeypatch, search_fixture._HandlerRepo(vector_s=0.0))
    assert failed["partial"] is True
    assert "partial" not in recovered and "partial_reason" not in recovered
    assert not recovered["note"]
    assert {unit["unit_id"] for unit in recovered["results"]} == {"L1", "V1"}


def test_fanout_keeps_a_child_failure_visible() -> None:
    unit = CodeUnit(unit_id="one", path="one.py", start_line=1, end_line=2)
    output = search_fanout.assemble(
        CodeSearchInput(query="how are queries executed", namespaces=["one", "two"]),
        ["one", "two"],
        {
            "one": (
                CodeSearchOutput(results=[unit], route="hybrid"),
                1,
            ),
            "two": (
                CodeSearchOutput(
                    results=[],
                    route="hybrid",
                    abstain=True,
                    abstain_code="no_match",
                    partial=True,
                    partial_reason="retrieval unavailable: vector",
                    epistemic="partial",
                    note="vector retrieval is unavailable",
                ),
                1,
            ),
        },
        [],
        [unit],
        10,
        timeout_ms=500,
        output_model=CodeSearchOutput,
    )
    assert output.partial is True and output.epistemic == "partial"
    assert output.partial_reason == "two: retrieval unavailable: vector"
    assert output.note == "two: vector retrieval is unavailable"


def test_a_failed_retrieval_is_not_saved_as_a_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import search_cursor

    one = CodeUnit(unit_id="one", path="one.py", start_line=1, end_line=2)
    two = CodeUnit(unit_id="two", path="two.py", start_line=1, end_line=2)
    result = CodeSearchResult(units=[one], pool=[one, two], retrieval_work={"vector_leg_failed": 1})
    stored: list[Any] = []
    monkeypatch.setattr(search_cursor, "enabled", lambda: True)
    monkeypatch.setattr(handler, "_search_params_hash", lambda *args: "params")
    monkeypatch.setattr(search_cursor, "put_pool", lambda *args, **kwargs: stored.append(args))
    monkeypatch.setattr(search_cursor, "encode", lambda **kwargs: "cursor")
    cursor = handler._issue_search_cursor(
        CodeSearchInput(query="how are queries executed"),
        "org",
        "namespace",
        result,
        run_id="trace",
        embedding_model="embedder",
        indexed_commit="c" * 40,
    )
    assert cursor == "" and stored == []
