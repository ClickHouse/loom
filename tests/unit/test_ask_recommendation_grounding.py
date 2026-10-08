"""memory.ask recommendation grounding: a recommendation-shaped
question opens with the user's recorded preferences, then tailors."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit
from server.memory.app.ask.question_shapes import is_recommendation_question


def test_cue_detector() -> None:
    yes = [
        "Can you recommend some resources where I can learn more about video editing?",
        "Can you suggest some accessories that would complement my photography setup?",
        "What should I cook for the dinner party?",
        "Any tips for my first marathon?",
        "Help me choose a laptop for school",
    ]
    no = ["What is my favorite color", "How many plants did I buy", "When did I move to Lisbon"]
    assert all(is_recommendation_question(q) for q in yes)
    assert not any(is_recommendation_question(q) for q in no)


class _FakeLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text="Premiere Pro tutorials [m1].")


def _hit(key: str, excerpt: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        rank_score=0.8,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace(llm=_FakeLLM(), settings=settings)
    e.search_output = MemorySearchOutput(
        results=[_hit("m1", "user edits video in Adobe Premiere Pro, advanced settings")],
        embedding_model="fake-embed",
    )

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return e.search_output

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    return e


async def _run(**overrides: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "Can you recommend some resources where I can learn more about video editing?",
    }
    base.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**base))


async def test_on_by_default_per_call_off_and_non_recommendation(env) -> None:
    await _run()
    assert "RECOMMENDATION question" in env.llm.calls[0][0]
    await _run(personalize=False)
    assert "RECOMMENDATION question" not in env.llm.calls[1][0]
    await _run(query="what is my favorite color")
    assert "RECOMMENDATION question" not in env.llm.calls[2][0]
