"""memory.ask default answer length: a deployment default for
max_answer_chars that spares breadth and count questions and yields to an
explicit caller value."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit


class _FakeLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user, max_tokens))
        return SimpleNamespace(text="Blue [m1].")


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
        results=[_hit("m1", "favorite color is blue"), _hit("m2", "favorite food is sushi")],
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
    monkeypatch.setattr(settings, "memory_ask_default_answer_chars", 0)
    return e


async def _run(**overrides: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "what is my favorite color",
    }
    base.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**base))


def _cap_clause(system: str) -> bool:
    return "Keep the answer within about" in system or "Keep the ANSWER line within about" in system


async def test_default_zero_leaves_the_answer_unbounded(env) -> None:
    await _run()
    system, _user, max_tokens = env.llm.calls[0]
    assert not _cap_clause(system)
    assert max_tokens >= 600


async def test_deployment_default_caps_a_single_fact_question(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_default_answer_chars", 1200)
    await _run()
    system, _user, max_tokens = env.llm.calls[0]
    assert "within about 1200 characters" in system
    assert max_tokens <= 600  # scaled down from the unbounded 600, floored by the mode


async def test_explicit_caller_value_beats_the_default(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_default_answer_chars", 1200)
    await _run(max_answer_chars=400)
    system, _user, _mt = env.llm.calls[0]
    assert "within about 400 characters" in system


async def test_breadth_questions_are_exempt(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_default_answer_chars", 1200)
    await _run(query="which colors and foods do I like")
    system, _user, _mt = env.llm.calls[0]
    assert not _cap_clause(system)


async def test_count_questions_are_exempt(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_default_answer_chars", 1200)
    await _run(query="how many colors have I mentioned")
    system, _user, _mt = env.llm.calls[0]
    assert "within about 1200" not in system


async def test_request_pins_the_default_off_over_a_deployment_default(env, monkeypatch) -> None:
    # A bench pairs 0 against 1200 on ONE server.
    monkeypatch.setattr(env.settings, "memory_ask_default_answer_chars", 1200)
    await _run(default_answer_chars=0)
    system, _user, max_tokens = env.llm.calls[0]
    assert not _cap_clause(system) and max_tokens >= 600


async def test_request_pins_a_default_on_with_the_settings_exemptions(env) -> None:
    await _run(default_answer_chars=1200)
    system, _user, _mt = env.llm.calls[0]
    assert "within about 1200 characters" in system
    # The pinned default keeps the setting's shape exemptions: breadth stays unbounded.
    await _run(query="which colors and foods do I like", default_answer_chars=1200)
    system2, _user, _mt = env.llm.calls[1]
    assert not _cap_clause(system2)
    # An explicit max_answer_chars still wins over the pinned default.
    await _run(default_answer_chars=1200, max_answer_chars=400)
    system3, _user, _mt = env.llm.calls[2]
    assert "within about 400 characters" in system3
