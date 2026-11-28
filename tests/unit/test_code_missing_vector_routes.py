"""Requested vector retrieval cannot disappear behind a display route name."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.api.models_search import CodeSearchInput
from server.code.app import search, search_cursor, search_memo
from server.code.app.search import handler
from server.code.domain.search_scope import build_scope
from server.code.domain.units import CodeUnit
from server.embedding import embedder as embedding
from server.llm import customer
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


class _ModelFilteringRepo(_RoutingRepo):
    def __init__(self, stored_model: str) -> None:
        super().__init__(vector_s=0.0)
        self.stored_model = stored_model
        self.lexical_models: list[str] = []
        self.vector_models: list[str] = []
        self.key_models: list[str] = []

    async def lexical_search_chunks(
        self, org: str, ns: str, model: str, *args: Any, **kwargs: Any
    ) -> Any:
        self.lexical_models.append(model)
        if model != self.stored_model:
            return []
        return await super().lexical_search_chunks(org, ns, model, *args, **kwargs)

    async def vector_search_chunks(
        self, org: str, ns: str, model: str, *args: Any, **kwargs: Any
    ) -> Any:
        self.vector_models.append(model)
        if model != "secondary":
            return []
        return await super().vector_search_chunks(org, ns, model, *args, **kwargs)

    async def chunks_by_keys(self, org: str, ns: str, model: str, keys: Any) -> Any:
        self.key_models.append(model)
        if model != self.stored_model:
            return []
        return [{"chunk_id": unit_id, "path": path, **fixture._UNIT} for path, unit_id in keys]


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


def _unavailable_local() -> Any:
    raise ImportError("fastembed is not installed")


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


@pytest.mark.parametrize("requested_model", ["", "ctx:served-model"])
@pytest.mark.parametrize("secondary_works", [False, True])
@pytest.mark.parametrize(
    ("configured_model", "configured_provider", "served_model"),
    [
        ("served-model", "compatible", "served-model"),
        ("text-embedding-3-small", "local", "local"),
    ],
)
def test_primary_initialization_failure_keeps_the_lexical_model_partition(
    monkeypatch: pytest.MonkeyPatch,
    requested_model: str,
    secondary_works: bool,
    configured_model: str,
    configured_provider: str,
    served_model: str,
) -> None:
    stored_model = requested_model or served_model
    repo = _ModelFilteringRepo(stored_model)
    secondary = fixture._Embedder()
    secondary.model_name = "secondary"
    monkeypatch.setattr(settings, "embedding_model", configured_model)
    monkeypatch.setattr(settings, "embedding_provider", configured_provider)
    unavailable = _unavailable_local if configured_provider == "local" else _unavailable_primary
    monkeypatch.setattr(fixture, "_Embedder", unavailable)
    monkeypatch.setattr(embedding, "allowed_vector_models", lambda: ["secondary"])
    monkeypatch.setattr(embedding, "get_secondary_embedder", lambda name: secondary)
    data, _ = fixture._handle(
        monkeypatch,
        repo,
        embedding_model=requested_model,
        vector_model="secondary" if secondary_works else "",
        vector_shortlist=0,
    )
    expected = {"L1", "V1"} if secondary_works else {"L1"}
    assert {unit["unit_id"] for unit in data["results"]} == expected
    assert repo.lexical_models == [stored_model]
    if secondary_works:
        assert "partial" not in data and not data["note"]
        assert repo.vector_models == ["secondary"]
    else:
        assert data["partial"] is True
        assert data["partial_reason"] == "retrieval unavailable: vector"
        assert repo.vector_models == []


@pytest.mark.parametrize(
    ("configured_model", "configured_provider", "served_model"),
    [
        ("served-model", "compatible", "served-model"),
        ("text-embedding-3-small", "local", "local"),
    ],
)
def test_continuations_keep_the_configured_model_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    configured_model: str,
    configured_provider: str,
    served_model: str,
) -> None:
    repo = _ModelFilteringRepo(served_model)
    for name, value in {
        "code_intel_enabled": True,
        "code_org": "o",
        "code_namespace": "n",
        "code_trace_log_enabled": False,
        "code_risk_annotations": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "embedding_model", configured_model)
    monkeypatch.setattr(settings, "embedding_provider", configured_provider)
    unavailable = _unavailable_local if configured_provider == "local" else _unavailable_primary
    monkeypatch.setattr(embedding, "get_embedder", unavailable)
    monkeypatch.setattr(handler, "CodeRepo", lambda: repo)
    request = CodeSearchInput(query=fixture._QUESTION, top_k=1, vector_model="secondary")
    params = search_cursor.params_hash(request, rerank="", embedding_model=served_model)
    pool = [
        CodeUnit(unit_id="L1", path="src/a.cpp", score=1.0),
        CodeUnit(unit_id="V1", path="src/b.cpp", score=0.9),
    ]
    payload = search_cursor.search_pool_payload(
        query=request.query,
        route="hybrid",
        model=served_model,
        params=params,
        served=1,
        pool=pool,
        calibration={},
    )
    # A continuation carries its original scope even when the search has no filters.
    cursor = search_cursor.encode(
        kind="s",
        run_id=f"run.{search_cursor.search_scope_digest(build_scope())}",
        offset=1,
        params=params,
        indexed_commit="c" * 40,
    )

    async def stored(*args: Any) -> Any:
        return {"payload": payload, "indexed_commit": "c" * 40}

    async def as_of(*args: Any) -> Any:
        return {"commit": "c" * 40}

    async def no_retrieval(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a continuation must not rerun retrieval")

    monkeypatch.setattr(search_cursor, "get_pool", stored)
    monkeypatch.setattr(handler, "index_as_of", as_of)
    monkeypatch.setattr(search, "search", no_retrieval)
    result = asyncio.run(handler.handle_code_search(request.model_copy(update={"cursor": cursor})))
    assert [unit.unit_id for unit in result.results] == ["V1"]
    assert result.results[0].body == "x" and repo.key_models == [served_model]
    assert result.retrieval_work["embedding_calls"] == 0


@pytest.mark.parametrize("hold_type", customer.HOLDS)
@pytest.mark.parametrize("continuation", [False, True])
def test_model_policy_holds_are_not_converted_to_partition_fallbacks(
    monkeypatch: pytest.MonkeyPatch, hold_type: Any, continuation: bool
) -> None:
    refusal = hold_type("model access is held")

    def held() -> Any:
        raise refusal

    monkeypatch.setattr(embedding, "get_embedder", held)
    monkeypatch.setattr(fixture, "_Embedder", held)
    with pytest.raises(hold_type) as caught:
        if continuation:
            handler._embedding_model_name()
        else:
            fixture._handle(monkeypatch, _RoutingRepo(vector_s=0.0))
    assert caught.value is refusal


@pytest.mark.parametrize("hold_type", customer.HOLDS)
def test_vector_execution_preserves_policy_holds(
    monkeypatch: pytest.MonkeyPatch, hold_type: Any
) -> None:
    refusal = hold_type("model access is held")
    stored: list[Any] = []

    class HeldEmbedder:
        model_name = "served-model"

        async def embed(self, text: str) -> Any:
            raise refusal

    async def miss(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(fixture, "_Embedder", HeldEmbedder)
    monkeypatch.setattr(search_memo, "enabled", lambda input: True)
    monkeypatch.setattr(search_memo, "get", miss)
    monkeypatch.setattr(search_memo, "put", lambda *args, **kwargs: stored.append(args))
    with pytest.raises(hold_type) as caught:
        fixture._handle(monkeypatch, _RoutingRepo(vector_s=0.0))
    assert caught.value is refusal and stored == []


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
