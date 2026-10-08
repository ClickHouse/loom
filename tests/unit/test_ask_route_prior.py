"""The consistency verdicts as a routing prior — the handler
reads the setting or the request's pin, and the ledger row names the route."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit


class _FakeLLM:
    provider = "fake"

    def __init__(self, model: str, text: str) -> None:
        self.model = model
        self.text = text
        self.calls = 0

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(text=self.text, model=self.model)


def _hit(key: str, sim: float) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"fact {key}",
        distance=1 - sim,
        similarity=sim,
        rank_score=sim,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace()
    # A weak top hit (0.6 < 0.70) with otherwise easy signals: the rule ladder
    # says strong / weak_top_hit — the route the measured prior puts at 19.4%.
    e.search_output = MemorySearchOutput(
        results=[_hit("m1", 0.6), _hit("m2", 0.5)],
        embedding_model="fake-embed",
        retrieval_confidence=0.7,
        retrieval_risk="medium",
        route_agreement=0.9,
    )
    e.strong = _FakeLLM("strong", "The colour is blue [m1].")
    e.cheap = _FakeLLM("cheap", "The colour is blue [m1].")

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return e.search_output

    async def ratio(org: str, ns: str) -> float:
        return 0.0

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_budget_ratio", ratio)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.strong)
    monkeypatch.setattr(
        llm_client, "get_llm_client_for_model", lambda m: e.cheap if m == "cheap" else e.strong
    )
    monkeypatch.setattr(settings, "memory_ask_synth_model", "strong")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "cheap")
    monkeypatch.setattr(settings, "memory_ask_consistency_check", False)
    monkeypatch.setattr(settings, "memory_ask_premise_check", False)
    monkeypatch.setattr(settings, "memory_ask_identifier_injection", False)
    monkeypatch.setattr(settings, "memory_ask_code_enrichment", False)
    monkeypatch.setattr(settings, "memory_ask_code_staleness", False)
    monkeypatch.setattr(settings, "memory_ask_synth_route_prior", "")
    monkeypatch.setattr(settings, "memory_ask_synth_route_prior_max_cheap", 0.10)
    e.settings = settings
    return e


async def _ask(**over: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "what colour is it",
    }
    base.update(over)
    return await handle_memory_ask(MemoryAskInput(**base))


@pytest.mark.asyncio
async def test_default_is_the_rule_only_router(env) -> None:
    out = await _ask()
    assert out.route_tier == "strong" and out.route_reason == "weak_top_hit"
    assert env.strong.calls == 1 and env.cheap.calls == 0


@pytest.mark.asyncio
async def test_setting_names_the_measured_prior_and_its_threshold(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_synth_route_prior", "measured")
    out = await _ask()
    # 19.4% is over the 0.10 default: the uncertain route stays strong.
    assert out.route_tier == "strong" and out.route_reason == "weak_top_hit"
    monkeypatch.setattr(env.settings, "memory_ask_synth_route_prior_max_cheap", 0.20)
    out2 = await _ask()
    assert out2.route_tier == "cheap" and out2.route_reason == "weak_top_hit:prior_cheap"
    assert out2.synth_model == "cheap" and env.cheap.calls == 1


@pytest.mark.asyncio
async def test_request_pins_the_prior_over_the_setting(env, monkeypatch) -> None:
    out = await _ask(route_prior="measured", route_prior_max_cheap=0.20)
    assert out.route_tier == "cheap" and out.route_reason == "weak_top_hit:prior_cheap"
    monkeypatch.setattr(env.settings, "memory_ask_synth_route_prior", "measured")
    monkeypatch.setattr(env.settings, "memory_ask_synth_route_prior_max_cheap", 0.20)
    off = await _ask(route_prior="off")
    assert off.route_tier == "strong" and off.route_reason == "weak_top_hit"
    custom = await _ask(route_prior='{"weak_top_hit": 0.5}', route_prior_max_cheap=0.01)
    assert custom.route_tier == "strong", "the request threshold binds the request prior"


@pytest.mark.asyncio
async def test_a_failed_cheap_answer_on_the_prior_route_still_escalates(env) -> None:
    env.cheap.text = "I don't know."  # uncited + hedged: the verifier fires
    out = await _ask(route_prior="measured", route_prior_max_cheap=0.20)
    assert out.route_tier == "strong" and out.escalated
    assert out.route_reason.startswith("weak_top_hit:prior_cheap:cheap_")
    assert env.cheap.calls == 1 and env.strong.calls == 1


def test_the_request_fields_are_projected_for_the_ledger() -> None:
    from server.observability.event_args import project_args

    args = project_args(
        MemoryAskInput(
            org="o",
            namespace="ns",
            agent="*",
            query="q",
            route_prior="measured",
            route_prior_max_cheap=0.2,
            default_answer_chars=1200,
            consistency="inline",
        ).model_dump(exclude_none=True)
    )
    assert args["route_prior_max_cheap"] == "0.2" and args["default_answer_chars"] == "1200"
    assert args["consistency"] == "inline"
