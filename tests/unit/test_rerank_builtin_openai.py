"""Unit tests for the builtin:openai LLM reranker.

The function calls Loom's LLM client behind a sentinel endpoint
(`builtin:openai`), parses a JSON `{"ranked": [int, ...]}` response,
and reorders candidates. We monkey-patch the client to avoid an
OpenAI dependency in unit tests.
"""

from __future__ import annotations

import pytest

from server.llm.client import LLMResult
from server.memory.app.rerank import RerankCandidate, rerank


def _cand(key: str, text: str = "x") -> RerankCandidate:
    return RerankCandidate(
        memory_key=key,
        namespace="ns",
        content_excerpt=text,
        memory_type="semantic",
        similarity=0.5,
    )


class _FakeLLM:
    """Stand-in for OpenAILLMClient. The builtin reranker's
    isinstance(FakeLLMClient) guard treats real FakeLLMClient as a
    pass-through; this stub is a different class, so it executes the
    JSON-parse path."""

    provider = "openai"
    model = "gpt-4o-mini"

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        self.calls.append((system, user, max_tokens))
        return LLMResult(
            text=self._response_text,
            tokens_in=20,
            tokens_out=10,
            provider=self.provider,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_builtin_openai_reranks_by_ranked_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM returns indices 2, 0; the candidates come back in that
    order, with the dropped index appended at the end."""
    llm = _FakeLLM('{"ranked": [2, 0]}')
    monkeypatch.setattr(
        "server.llm.client.get_llm_client",
        lambda: llm,
    )

    candidates = [_cand("a"), _cand("b"), _cand("c")]
    out = await rerank(
        endpoint="builtin:openai",
        prompt_template="",
        query="anything",
        candidates=candidates,
    )
    assert [c.memory_key for c in out] == ["c", "a", "b"]
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_builtin_openai_fails_open_on_malformed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LLM returns garbage, the reranker leaves the input order
    unchanged — never silently drops or randomly reorders hits."""
    llm = _FakeLLM("oops not json")
    monkeypatch.setattr(
        "server.llm.client.get_llm_client",
        lambda: llm,
    )

    candidates = [_cand("a"), _cand("b")]
    out = await rerank(
        endpoint="builtin:openai",
        prompt_template="",
        query="anything",
        candidates=candidates,
    )
    assert [c.memory_key for c in out] == ["a", "b"]


@pytest.mark.asyncio
async def test_builtin_openai_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`builtin:openai:gpt-4o` answers from the per-model client for gpt-4o;
    the shared default client is neither called nor re-pointed (mutating the
    shared client would race concurrent requests into each other's model)."""
    shared = _FakeLLM('{"ranked": [0]}')
    override = _FakeLLM('{"ranked": [1, 0]}')
    override.model = "gpt-4o"
    asked: list[str | None] = []

    def _for_model(model: str | None) -> _FakeLLM:
        asked.append(model)
        return override

    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: shared)
    monkeypatch.setattr("server.llm.client.get_llm_client_for_model", _for_model)

    out = await rerank(
        endpoint="builtin:openai:gpt-4o",
        prompt_template="",
        query="anything",
        candidates=[_cand("a"), _cand("b")],
    )
    assert asked == ["gpt-4o"]
    assert [c.memory_key for c in out] == ["b", "a"]
    assert len(override.calls) == 1 and shared.calls == []
    assert shared.model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_builtin_openai_skips_when_fake_llm_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LOOM_LLM_PROVIDER=fake (unit/local), reranker no-ops so
    tests don't depend on an OpenAI key."""
    from server.llm.fake import FakeLLMClient

    llm = FakeLLMClient()
    monkeypatch.setattr(
        "server.llm.client.get_llm_client",
        lambda: llm,
    )

    candidates = [_cand("a"), _cand("b"), _cand("c")]
    out = await rerank(
        endpoint="builtin:openai",
        prompt_template="",
        query="anything",
        candidates=candidates,
    )
    assert [c.memory_key for c in out] == ["a", "b", "c"]
