"""Requested vector retrieval cannot disappear behind a display route name."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app import search, search_memo
from server.code.domain.search_scope import build_scope
from server.embedding import embedder as embedding
from server.settings import settings
from tests.unit import test_code_search_deadline as fixture


class _RoutingRepo(fixture._HandlerRepo):
    async def scope_file_count(self, *args: Any, **kwargs: Any) -> int:
        return 1

    async def pr_corpus_stats(self, org: str, ns: str, tokens: list[str], **kwargs: Any) -> Any:
        return [1] * len(tokens), 100

    async def pr_history_search(self, *args: Any, **kwargs: Any) -> Any:
        return [
            {
                "pr_number": 42,
                "title": "Bound query memory",
                "files": ["src/a.cpp"],
                "pr_score": 4.0,
            }
        ]

    async def chunks_for_paths(self, *args: Any, **kwargs: Any) -> Any:
        return [{"chunk_id": "L1", "path": "src/a.cpp", **fixture._UNIT}]

    async def symbols_by_name(self, org: str, ns: str, name: str) -> Any:
        return [
            {
                "symbol_id": "symbol",
                "qualified_name": name,
                "path": "src/a.cpp",
                "start_line": 1,
                "end_line": 2,
                "kind": "function",
            }
        ]


@pytest.fixture(autouse=True)
def _plain_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "code_fusion_signals": False,
        "code_search_card_leg": False,
        "code_search_concept_expand_model": "",
        "code_search_hyde_model": "",
        "code_search_pr_leg": "off",
        "code_search_history_route": "off",
        "code_search_vector_model_default": "",
    }.items():
        monkeypatch.setattr(settings, name, value)


def _run_pipeline(**kwargs: Any) -> Any:
    return asyncio.run(
        search.search(
            _RoutingRepo(vector_s=0.0),
            "org",
            "namespace",
            query=kwargs.pop("query", fixture._QUESTION),
            embedder=None,
            bodies="none",
            llm_legs="none",
            deadline_ms=0,
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    ("arguments", "expected_route"),
    [
        ({"legs": "vector"}, "vector_only"),
        ({"scope": build_scope(path_globs=["src/**"])}, "hybrid+scoped"),
        ({"scope": build_scope(pattern="memory")}, "hybrid+pattern"),
    ],
)
def test_missing_vector_is_disclosed_independently_of_display_route(
    arguments: dict[str, Any], expected_route: str
) -> None:
    result = _run_pipeline(**arguments)
    assert result.route == expected_route
    assert result.partial is True and result.epistemic == "partial"
    assert result.partial_reason == "retrieval unavailable: vector"
    assert result.retrieval_work["vector_leg_failed"] == 1
    assert "vector" in result.note


def test_history_enrichment_cannot_hide_missing_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_search_pr_leg", "always")
    monkeypatch.setattr(settings, "code_search_pr_leg_min_prs", 1)
    result = _run_pipeline()
    assert result.route == "hybrid+history"
    assert result.partial is True and result.epistemic == "partial"
    assert result.partial_reason == "retrieval unavailable: vector"


@pytest.mark.parametrize("arguments", [{"legs": "lexical"}, {"query": "MemoryTracker::allocImpl"}])
def test_missing_embedder_does_not_taint_routes_that_need_no_vector(
    arguments: dict[str, Any],
) -> None:
    result = _run_pipeline(**arguments)
    assert result.units
    assert not result.partial and not result.note and not result.epistemic
    assert "vector_leg_failed" not in result.retrieval_work


def _unavailable_primary() -> Any:
    raise RuntimeError("primary embedder initialization failed")


def test_a_valid_secondary_vector_needs_no_primary_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secondary = fixture._Embedder()
    monkeypatch.setattr(fixture, "_Embedder", _unavailable_primary)
    monkeypatch.setattr(embedding, "allowed_vector_models", lambda: ["secondary"])
    monkeypatch.setattr(embedding, "get_secondary_embedder", lambda name: secondary)
    data, _ = fixture._handle(
        monkeypatch, _RoutingRepo(vector_s=0.0), vector_model="secondary", vector_shortlist=0
    )
    assert any(unit["unit_id"] == "V1" for unit in data["results"])
    assert "partial" not in data and not data["note"]
    assert "vector_leg_failed" not in data["retrieval_work"]


@pytest.mark.parametrize(
    "arguments", [{"legs": "vector"}, {"path_globs": ["src/**"]}, {"pattern": "memory"}]
)
def test_missing_vector_results_are_never_memoized(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    stored: list[Any] = []

    async def miss(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(fixture, "_Embedder", _unavailable_primary)
    monkeypatch.setattr(search_memo, "enabled", lambda input: True)
    monkeypatch.setattr(search_memo, "get", miss)
    monkeypatch.setattr(search_memo, "put", lambda *args, **kwargs: stored.append(args))
    data, _ = fixture._handle(monkeypatch, _RoutingRepo(vector_s=0.0), **arguments)
    assert data["partial"] is True and stored == []


def test_real_fanout_preserves_missing_vector_from_each_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz

    async def readable(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(authz, "row_in_scope", readable)
    monkeypatch.setattr(fixture, "_Embedder", _unavailable_primary)
    data, _ = fixture._handle(
        monkeypatch, _RoutingRepo(vector_s=0.0), namespaces=["one", "two"], legs="vector"
    )
    assert data["partial"] is True and data["epistemic"] == "partial"
    assert data["partial_reason"] == (
        "one: retrieval unavailable: vector; two: retrieval unavailable: vector"
    )
