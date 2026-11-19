"""Repository answers disclose incomplete synthesis and retrieval before caching."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app import answers, answers_entry
from server.code.app.answers_entry import _AskCtx, cache_key_for
from server.code.app.search import CodeSearchResult
from server.code.domain.units import CodeUnit


async def _grounded(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    *,
    stop_reason: str = "end_turn",
    retrieval: CodeSearchResult | None = None,
) -> Any:
    async def no_expansion(*args: Any, **kwargs: Any) -> list[CodeUnit]:
        return []

    async def no_memory(*args: Any, **kwargs: Any) -> answers.MemoryContext:
        return answers.MemoryContext()

    async def public_memory(*args: Any, **kwargs: Any) -> bool:
        return True

    class LLM:
        async def complete(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(text=text, stop_reason=stop_reason)

    monkeypatch.setattr(answers, "_expand_mechanism", no_expansion)
    monkeypatch.setattr(answers, "_inject_memory_context", no_memory)
    monkeypatch.setattr(answers, "_memory_context_cacheable", public_memory)
    context = _AskCtx(
        repo=SimpleNamespace(),
        org="test-org",
        ns="test-code",
        question="Explain the repository's configuration.",
        qvec=[1.0, 0.0],
        model="fake-test",
        key=cache_key_for({}, "fake-test"),
        index_head="base",
        started=0.0,
        cost_at_start=0.0,
    )
    if retrieval is None:
        retrieval = CodeSearchResult(
            units=[CodeUnit(path="docs/configuration.md", start_line=1, end_line=8, body="config")]
        )
    return await answers._grounded_answer(context, LLM(), retrieval, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("paragraph", ["A" * 8500, "설정" * 4300])
async def test_complete_answer_keeps_text_past_character_limit(
    monkeypatch: pytest.MonkeyPatch, paragraph: str
) -> None:
    result = await _grounded(monkeypatch, paragraph + "\nCOVERAGE: full")
    assert result.answer == paragraph
    assert result.coverage == "full"
    assert result.memoize


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason",
    ["max_tokens", "length", "content_filter", "refusal", "content_filtered", "guardrail_intervened"],
)
async def test_cut_answer_discloses_partial_and_is_not_memoized(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str
) -> None:
    result = await _grounded(monkeypatch, "Incomplete answer.\nCOVERAGE: full", stop_reason=stop_reason)
    assert result.partial
    assert result.partial_reason
    refused = stop_reason in {"content_filter", "refusal", "content_filtered", "guardrail_intervened"}
    assert result.coverage == ("none" if refused else "partial")
    if refused:
        assert result.abstain
        assert result.answer == ""
    assert not result.memoize


@pytest.mark.asyncio
async def test_degraded_retrieval_cannot_be_memoized_as_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval = CodeSearchResult(
        units=[CodeUnit(path="app.py", start_line=1, end_line=8, body="app")],
        epistemic="partial",
        retrieval_work={"vector_leg_failed": 1},
    )
    result = await _grounded(monkeypatch, "Answer.\nCOVERAGE: full", retrieval=retrieval)
    assert result.partial
    assert result.coverage == "partial"
    assert not result.memoize


def test_answer_prompts_cover_the_indexed_repository_and_its_documents() -> None:
    assert "ClickHouse" not in answers_entry._EXPAND_SYSTEM
    assert "documentation" in answers_entry._SYNTH_SYSTEM
    assert "untrusted" in answers_entry._SYNTH_SYSTEM


@pytest.mark.asyncio
async def test_embedding_failure_uses_lexical_evidence_once_without_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedEmbedder:
        model_name = "failed-test-embedding"
        calls = 0

        async def embed(self, text: str) -> list[float]:
            self.calls += 1
            raise RuntimeError("private-credential-message")

    async def fresh(context: _AskCtx, **kwargs: Any) -> answers_entry.AskResult:
        assert context.model == "failed-test-embedding"
        assert kwargs["embedder"] is None
        assert kwargs["search_kwargs"]["embedding_model"] == "failed-test-embedding"
        assert not kwargs["cache_llm"]
        return answers_entry.AskResult(answer="Lexical evidence.", coverage="full", memoize=True)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A failed embedding cannot access the answer cache")

    monkeypatch.setattr(answers, "_answer_fresh", fresh)
    monkeypatch.setattr(answers, "_serve_from_cache", forbidden)
    monkeypatch.setattr(answers, "_remember", forbidden)
    embedder = FailedEmbedder()
    result = await answers.ask(
        SimpleNamespace(), "test-org", "test-code", question="Explain config.",
        embedder=embedder, llm=SimpleNamespace(), search_kwargs={},
    )
    assert embedder.calls == 1
    assert result.partial and not result.memoize
    assert result.coverage == "partial"
    assert result.cache == "bypassed"
    assert "private-credential-message" not in result.partial_reason + result.note


@pytest.mark.asyncio
async def test_partial_abstention_is_not_written_as_a_negative_cache_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import answers_cache

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Degraded retrieval is not proof of no answer")

    monkeypatch.setattr(answers_cache, "_store", forbidden)
    result = answers_entry.AskResult(
        partial=True, source="abstained", abstain=True, abstain_reason="synthesis_unavailable"
    )
    await answers_cache._remember(SimpleNamespace(), result, None)


def test_ranking_changes_invalidate_cached_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    original = answers_entry.prompt_version()
    with monkeypatch.context() as changed:
        changed.setattr(settings, "code_search_prose_penalty", 0.4)
        assert answers_entry.prompt_version() != original
    with monkeypatch.context() as changed:
        changed.setattr(settings, "code_search_kind_prior", "off")
        assert answers_entry.prompt_version() != original


@pytest.mark.asyncio
async def test_wire_response_keeps_incomplete_retrieval_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import ask as handler
    from server.code.api.models_search import CodeAskInput

    async def thresholds(*args: Any) -> tuple[float, float]:
        return (0.3, 0.2)

    async def as_of(*args: Any) -> dict[str, Any]:
        return {}

    async def answer(*args: Any, **kwargs: Any) -> answers_entry.AskResult:
        return answers_entry.AskResult(
            answer="Available evidence.", partial=True, partial_reason="vector unavailable",
            note="Semantic retrieval was unavailable.", coverage="partial",
        )

    monkeypatch.setattr(handler, "require_code_intel", lambda: None)
    monkeypatch.setattr(handler, "resolve_code_identity", lambda *a: ("test-org", "test-code"))
    monkeypatch.setattr(handler, "CodeRepo", SimpleNamespace)
    monkeypatch.setattr(handler, "resolve_code_thresholds", thresholds)
    monkeypatch.setattr(handler, "index_as_of", as_of)
    monkeypatch.setattr(handler, "record_code_ask_cache", lambda **k: None)
    monkeypatch.setattr(handler, "schedule_code_trace", lambda **k: None)
    monkeypatch.setattr(handler, "set_code_span_attributes", lambda **k: None)
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: SimpleNamespace(model_name="fake"))
    monkeypatch.setattr("server.observability.langfuse_scores.record_ask_scores", lambda **k: None)
    monkeypatch.setattr(answers, "ask", answer)
    result = await handler.handle_code_ask(CodeAskInput(question="Where is config?", synthesize=False))
    payload = result.model_dump()
    assert payload["partial"]
    assert payload["partial_reason"] == "vector unavailable"
    assert payload["coverage"] == "partial"
    assert payload["note"] == "Semantic retrieval was unavailable."
