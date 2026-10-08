"""An ask whose deep pass is known before synthesis (depth="raw",
or the temporal trigger) awaits the selector and synthesizes ONCE with the raw
turns; the excerpt synthesis runs only when the block is empty or the deep
answer fails the citation contract. The selector runs on its own tier."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit

pytestmark = pytest.mark.asyncio

_SELECTOR_MARK = "worth opening at full detail"  # the selector's system prompt
_RAW_BLOCK = "[user @ 2023-05-21 10:00] I met Samantha at the beach outing [m1]"


class _RecordingLLM:
    """Answers synthesis with `text`; answers the selector with a JSON pick."""

    provider = "fake"

    def __init__(self, text: str, model: str = "fake-llm") -> None:
        self._text = text
        self.model = model
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        if _SELECTOR_MARK in system:
            return SimpleNamespace(text=json.dumps({"picks": [1]}))
        return SimpleNamespace(text=self._text)

    def synth_calls(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if _SELECTOR_MARK not in c[0]]

    def selector_calls(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if _SELECTOR_MARK in c[0]]


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
    import server.memory.app.deep_evidence as deep
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace()
    e.llm = _RecordingLLM("She met Samantha on 21 May 2023 [m1].")
    e.turns_block = _RAW_BLOCK
    e.settings = settings

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return MemorySearchOutput(
            results=[_hit("m1", "met Samantha at the beach"), _hit("m2", "likes hiking")],
            embedding_model="fake-embed",
        )

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    async def fake_fetch(**kw: Any) -> list[Any]:
        if not e.turns_block:
            return []
        return [
            deep.EvidenceTurn(
                memory_key="m1",
                role="user",
                observed_at="2023-05-21 10:00",
                content="I met Samantha at the beach outing",
            )
        ]

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(deep, "fetch_raw_evidence", fake_fetch)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    monkeypatch.setattr(settings, "memory_ask_deep_select_tier", "cheap")
    monkeypatch.setattr(settings, "memory_ask_consistency_check", False, raising=False)
    monkeypatch.setattr(settings, "memory_ask_precedents", False, raising=False)
    return e


async def _ask(**overrides: Any) -> Any:
    from server.memory.app.ask.handler import handle_memory_ask

    body: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "when did she meet Samantha",
        "decompose": "off",
    }
    body.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**body))


async def test_deep_first_synthesizes_once_with_the_raw_turns(env) -> None:
    out = await _ask(depth="raw")
    assert out.source == "synthesized" and out.deep_fired
    synth = env.llm.synth_calls()
    assert len(synth) == 1, "one synthesis, not excerpt-then-deep"
    assert "beach outing" in synth[0][1], "the single synthesis carried the raw turns"
    assert out.depth == "raw"
    assert out.deep_first == "single"
    assert out.answer.startswith("She met Samantha")


async def test_empty_block_falls_back_to_the_excerpt_synthesis(env) -> None:
    env.turns_block = ""
    out = await _ask(depth="raw")
    assert out.source == "synthesized" and out.deep_fired
    synth = env.llm.synth_calls()
    assert len(synth) == 1
    assert "beach outing" not in synth[0][1], "no raw block: the excerpt synthesis alone"
    assert out.depth == "excerpt"
    assert out.deep_first == "empty_block"


async def test_a_deep_answer_that_fails_the_contract_runs_the_excerpt_synthesis(env) -> None:
    """The keep-or-replace rule needs the excerpt answer only when
    the deep answer is empty or fails the citation contract; then it runs."""
    env.llm._text = "The memories do not record when this happened."  # uncited hedge
    out = await _ask(depth="raw")
    synth = env.llm.synth_calls()
    assert len(synth) == 2, "deep failed the contract → excerpt synthesis ran"
    assert "beach outing" in synth[0][1] and "beach outing" not in synth[1][1]
    assert out.deep_first == "deep_failed"


async def test_request_off_keeps_the_serial_order(env) -> None:
    out = await _ask(depth="raw", deep_first=False)
    synth = env.llm.synth_calls()
    assert len(synth) == 2, "first synthesis beside the selector, then the deep one"
    assert "beach outing" not in synth[0][1] and "beach outing" in synth[1][1]
    assert out.depth == "raw"
    assert out.deep_first == ""


async def test_temporal_trigger_takes_the_deep_first_order(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_temporal_deep_select", "llm")
    out = await _ask(query="When did she meet Samantha?")
    assert out.deep_fired and out.deep_reason == "temporal"
    assert len(env.llm.synth_calls()) == 1
    assert out.deep_first == "single"


async def test_selector_runs_on_the_cheap_client_when_configured(env, monkeypatch) -> None:
    import server.llm.client as llm_client
    import server.observability.budget as budget

    strong = _RecordingLLM("She met Samantha on 21 May 2023 [m1].", model="strong-model")
    cheap = _RecordingLLM("Blue [m1].", model="cheap-model")

    def for_model(model: str | None) -> Any:
        return {"strong-model": strong, "cheap-model": cheap}[model]

    async def ratio(org: str, ns: str) -> float:
        return 0.1

    monkeypatch.setattr(llm_client, "get_llm_client_for_model", for_model)
    monkeypatch.setattr(budget, "namespace_budget_ratio", ratio)
    monkeypatch.setattr(env.settings, "memory_ask_synth_model", "strong-model")
    monkeypatch.setattr(env.settings, "memory_ask_synth_model_cheap", "cheap-model")
    # A breadth question routes strong (synth_policy), so the pre-start runs.
    out = await _ask(query="which places and people did she meet and when", depth="raw")
    assert out.route_tier == "strong"
    assert len(cheap.selector_calls()) == 1, "the selector went to the cheap client"
    assert strong.selector_calls() == []
    assert len(strong.synth_calls()) == 1 and cheap.synth_calls() == []

    cheap.calls.clear()
    strong.calls.clear()
    monkeypatch.setattr(env.settings, "memory_ask_deep_select_tier", "routed")
    out = await _ask(query="which places and people did she meet and when", depth="raw")
    assert len(strong.selector_calls()) == 1, "routed: the synthesizing client selects"
    assert cheap.selector_calls() == []


async def test_selection_is_awaited_before_the_single_synthesis(env) -> None:
    """Under deep-first the synthesis does not start until the selection
    (and the raw read) has finished, because the synthesis needs the block."""
    order: list[str] = []
    inner = env.llm.complete

    async def observed(system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        tag = "select" if _SELECTOR_MARK in system else "synth"
        order.append(f"{tag}_start")
        await asyncio.sleep(0.01)
        order.append(f"{tag}_end")
        return await inner(system, user, max_tokens, **kw)

    env.llm.complete = observed  # type: ignore[method-assign]
    await _ask(depth="raw")
    assert order.index("select_end") < order.index("synth_start"), order


async def test_per_request_override_both_ways(env) -> None:
    out = await _ask(depth="raw", deep_first=False)
    assert len(env.llm.synth_calls()) == 2 and out.deep_first == ""
    env.llm.calls.clear()
    out = await _ask(depth="raw", deep_first=True)
    assert len(env.llm.synth_calls()) == 1 and out.deep_first == "single"
