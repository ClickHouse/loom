"""Unavailable providers retain the configured lexical partition through code.ask."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.api.models_search import CodeAskInput
from server.code.app import ask as handler
from server.settings import settings
from tests.unit import test_code_search_deadline as fixture


class _PartitionRepo(fixture._HandlerRepo):
    def __init__(self, stored_model: str) -> None:
        super().__init__(vector_s=0.0)
        self.stored_model = stored_model
        self.models: list[str] = []
        self.returned: list[str] = []

    async def lexical_search_chunks(
        self, org: str, ns: str, model: str, *args: Any, **kwargs: Any
    ) -> Any:
        self.models.append(model)
        if model != self.stored_model:
            return []
        rows = await super().lexical_search_chunks(org, ns, model, *args, **kwargs)
        self.returned.extend(row["chunk_id"] for row in rows)
        return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "logical_model", "stored_model"),
    [
        ("local", "text-embedding-3-small", "local"),
        ("fake", "text-embedding-3-small", "fake-ngram-768"),
        ("compatible", "configured-model", "configured-model"),
    ],
)
async def test_factory_failure_reads_the_canonical_lexical_partition(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    logical_model: str,
    stored_model: str,
) -> None:
    repo = _PartitionRepo(stored_model)
    trace_models: list[str] = []

    def unavailable() -> Any:
        raise ImportError("provider client is unavailable")

    async def thresholds(*args: Any) -> tuple[float, float]:
        return 0.0, 0.0

    async def as_of(*args: Any) -> dict[str, Any]:
        return {}

    for name, value in {
        "embedding_provider": provider,
        "embedding_model": logical_model,
        "embedding_dim": 768,
        "code_fusion_signals": False,
        "code_search_card_leg": False,
        "code_search_concept_expand_model": "",
        "code_search_hyde_model": "",
        "code_search_pr_leg": "off",
        "code_search_history_route": "off",
        "code_search_vector_model_default": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(handler, "require_code_intel", lambda: None)
    monkeypatch.setattr(handler, "resolve_code_identity", lambda *a: ("test-org", "test-code"))
    monkeypatch.setattr(handler, "CodeRepo", lambda: repo)
    monkeypatch.setattr(handler, "resolve_code_thresholds", thresholds)
    monkeypatch.setattr(handler, "index_as_of", as_of)
    monkeypatch.setattr(handler, "record_code_ask_cache", lambda **k: None)
    monkeypatch.setattr(handler, "set_code_span_attributes", lambda **k: None)
    monkeypatch.setattr(
        handler, "schedule_code_trace", lambda **k: trace_models.append(k["embedding_model"])
    )
    monkeypatch.setattr("server.embedding.embedder.get_embedder", unavailable)
    monkeypatch.setattr("server.observability.langfuse_scores.record_ask_scores", lambda **k: None)
    monkeypatch.setattr(
        handler,
        "_ask_search_kwargs",
        lambda *a: {
            "legs": "lexical",
            "bodies": "none",
            "llm_legs": "none",
            "deadline_ms": 0,
            "abstain_threshold": 0.0,
            "noise_floor": 0.0,
            "rerank_endpoint": "none",
        },
    )
    result = await handler.handle_code_ask(
        CodeAskInput(question=fixture._QUESTION, synthesize=False)
    )
    assert set(repo.returned) == {"L1"}
    assert repo.models and set(repo.models) == {stored_model}
    assert trace_models == [stored_model]
    assert result.partial and result.cache == "bypassed"
