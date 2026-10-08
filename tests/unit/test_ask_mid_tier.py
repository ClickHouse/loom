"""A MID synthesis tier between cheap and strong — the handler
builds it from the setting or the request's pin, the router hands it the
uncertain strong routes, a failed mid answer escalates, and the ledger row
names the tier."""

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
        self.stages: list[str] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        from server.observability.genai import current_stage

        self.calls += 1
        self.stages.append(str(current_stage() or ""))
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
    # A weak top hit (0.6 < 0.70) with otherwise easy signals: strong /
    # weak_top_hit under the rules — one of the three uncertain routes.
    e.search_output = MemorySearchOutput(
        results=[_hit("m1", 0.6), _hit("m2", 0.5)],
        embedding_model="fake-embed",
        retrieval_confidence=0.7,
        retrieval_risk="medium",
        route_agreement=0.9,
    )
    e.strong = _FakeLLM("strong", "The colour is blue [m1].")
    e.cheap = _FakeLLM("cheap", "The colour is blue [m1].")
    e.mid = _FakeLLM("claude-sonnet-5", "The colour is blue [m1].")
    e.haiku = _FakeLLM("claude-haiku-4-5-20251001", "The colour is blue [m1].")
    by_model = {"cheap": e.cheap, "claude-sonnet-5": e.mid, "claude-haiku-4-5-20251001": e.haiku}

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return e.search_output

    async def ratio(org: str, ns: str) -> float:
        return 0.0

    async def over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_budget_ratio", ratio)
    monkeypatch.setattr(budget, "namespace_over_cap", over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.strong)
    monkeypatch.setattr(llm_client, "get_llm_client_for_model", lambda m: by_model.get(m, e.strong))
    monkeypatch.setattr(settings, "memory_ask_synth_model", "strong")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "cheap")
    monkeypatch.setattr(settings, "memory_ask_mid_model", "")
    monkeypatch.setattr(settings, "memory_ask_synth_mid_routes", "uncertain")
    monkeypatch.setattr(settings, "memory_ask_consistency_check", False)
    monkeypatch.setattr(settings, "memory_ask_premise_check", False)
    monkeypatch.setattr(settings, "memory_ask_identifier_injection", False)
    monkeypatch.setattr(settings, "memory_ask_code_enrichment", False)
    monkeypatch.setattr(settings, "memory_ask_code_staleness", False)
    monkeypatch.setattr(settings, "memory_ask_synth_route_prior", "")
    monkeypatch.setattr(settings, "memory_ask_llm_cache_default", False)
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
async def test_no_mid_model_is_the_two_tier_cascade(env) -> None:
    out = await _ask()
    assert out.route_tier == "strong" and out.route_reason == "weak_top_hit"
    assert env.strong.calls == 1 and env.mid.calls == 0


@pytest.mark.asyncio
async def test_the_setting_hands_the_uncertain_route_to_the_mid_model(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_mid_model", "claude-sonnet-5")
    out = await _ask()
    assert out.route_tier == "mid" and out.route_reason == "weak_top_hit:mid"
    assert out.synth_model == "claude-sonnet-5" and not out.escalated
    assert env.mid.calls == 1 and env.strong.calls == 0 and env.cheap.calls == 0
    # The tier is its own genai series (ledger / metrics label).
    assert env.mid.stages == ["ask_synthesize_mid"]


@pytest.mark.asyncio
async def test_the_request_pins_the_mid_model_and_off_is_the_control(env, monkeypatch) -> None:
    out = await _ask(mid_model="claude-haiku-4-5-20251001")
    assert out.route_tier == "mid" and out.synth_model == "claude-haiku-4-5-20251001"
    assert env.haiku.calls == 1
    monkeypatch.setattr(env.settings, "memory_ask_mid_model", "claude-sonnet-5")
    off = await _ask(mid_model="off")
    assert off.route_tier == "strong" and off.route_reason == "weak_top_hit"
    assert env.mid.calls == 0


@pytest.mark.asyncio
async def test_mid_routes_pin_narrows_and_off_disables(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_mid_model", "claude-sonnet-5")
    out = await _ask(mid_routes="low_confidence")
    assert out.route_tier == "strong" and out.route_reason == "weak_top_hit"
    out2 = await _ask(mid_routes="off")
    assert out2.route_tier == "strong"
    out3 = await _ask(mid_routes="weak_top_hit")
    assert out3.route_tier == "mid"


@pytest.mark.asyncio
async def test_easy_questions_still_go_cheap_with_a_mid_tier(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_mid_model", "claude-sonnet-5")
    env.search_output.results[0].similarity = 0.9  # a clear leader: easy_question
    out = await _ask()
    assert out.route_tier == "cheap" and out.route_reason == "easy_question"
    assert env.cheap.calls == 1 and env.mid.calls == 0


@pytest.mark.asyncio
async def test_a_mid_tier_without_a_cheap_tier_keeps_easy_questions_strong(
    env, monkeypatch
) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_synth_model_cheap", "")
    monkeypatch.setattr(env.settings, "memory_ask_mid_model", "claude-sonnet-5")
    env.search_output.results[0].similarity = 0.9
    out = await _ask()
    assert out.route_tier == "strong" and out.route_reason == "easy_question:no_cheap_tier"
    assert env.strong.calls == 1 and env.cheap.calls == 0 and env.mid.calls == 0
    # ...and the uncertain route still reaches the mid model.
    env.search_output.results[0].similarity = 0.6
    out2 = await _ask()
    assert out2.route_tier == "mid" and out2.route_reason == "weak_top_hit:mid"


@pytest.mark.asyncio
async def test_a_failed_mid_answer_escalates_to_strong_once(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_mid_model", "claude-sonnet-5")
    env.mid.text = "I don't know."  # uncited + hedged: the verifier fires
    out = await _ask()
    assert out.route_tier == "strong" and out.escalated
    assert out.route_reason == "weak_top_hit:mid:mid_uncited"
    assert out.synth_model == "strong"
    assert env.mid.calls == 1 and env.strong.calls == 1


@pytest.mark.asyncio
async def test_the_strong_model_as_mid_is_no_mid_tier(env, monkeypatch) -> None:
    monkeypatch.setattr(env.settings, "memory_ask_mid_model", "strong")
    out = await _ask()
    assert out.route_tier == "strong" and out.route_reason == "weak_top_hit"


def test_the_ledger_row_names_the_tier_and_the_pins() -> None:
    from server.memory.api.models_ask import MemoryAskOutput
    from server.observability.event_args import project_args, project_result

    args = project_args(
        MemoryAskInput(
            org="o",
            namespace="ns",
            agent="*",
            query="q",
            mid_model="claude-sonnet-5",
            mid_routes="uncertain",
        ).model_dump(exclude_none=True)
    )
    assert args["mid_model"] == "claude-sonnet-5" and args["mid_routes"] == "uncertain"
    out = project_result(
        MemoryAskOutput(
            answer="a",
            source="synthesized",
            route_tier="mid",
            route_reason="weak_top_hit:mid",
            synth_model="claude-sonnet-5",
        )
    )
    assert out["out_route_tier"] == "mid" and out["out_route_reason"] == "weak_top_hit:mid"


def test_the_mid_stage_is_registered_and_priced() -> None:
    from server.observability import genai
    from server.observability.cost_accumulator import turn_cost_usd

    with genai.llm_stage("ask_synthesize_mid"):
        assert genai.current_stage() == "ask_synthesize_mid"
    assert "ask_synthesize_mid" in genai._KNOWN_STAGES
    # Both candidate mid models are in the price table (a model the table
    # lacks would price at 0 and the arm's saving would be fiction).
    for model in ("claude-sonnet-5", "claude-haiku-4-5-20251001"):
        assert turn_cost_usd(model, input_tokens=1000, output_tokens=100) > 0


# ---- the deep-first order on the mid tier -----------------------------------

_SELECTOR_MARK = "worth opening at full detail"


class _DeepLLM(_FakeLLM):
    """Answers the selector with a JSON pick and synthesis with `text`; records
    which calls carried a raw-evidence block."""

    def __init__(self, model: str, text: str, deep_text: str | None = None) -> None:
        super().__init__(model, text)
        self.deep_text = deep_text
        self.raw_calls = 0

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        import json as _json

        if _SELECTOR_MARK in system:
            return SimpleNamespace(text=_json.dumps({"picks": [1]}), model=self.model)
        if "I met Samantha at the beach outing" in user:
            self.raw_calls += 1
            if self.deep_text is not None:
                self.calls += 1
                return SimpleNamespace(text=self.deep_text, model=self.model)
        return await super().complete(system, user, max_tokens, **kw)


@pytest.fixture
def deep_env(env, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.deep_evidence as deep

    env.mid = _DeepLLM("claude-sonnet-5", "She met Samantha on 21 May 2023 [m1].")
    env.strong = _DeepLLM("strong", "She met Samantha on 21 May 2023 [m1].")
    by_model = {"cheap": env.cheap, "claude-sonnet-5": env.mid}
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: env.strong)
    monkeypatch.setattr(
        llm_client, "get_llm_client_for_model", lambda m: by_model.get(m, env.strong)
    )

    async def fake_fetch(**kw: Any) -> list[Any]:
        return [
            deep.EvidenceTurn(
                memory_key="m1",
                role="user",
                observed_at="2023-05-21 10:00",
                content="I met Samantha at the beach outing",
            )
        ]

    monkeypatch.setattr(deep, "fetch_raw_evidence", fake_fetch)
    monkeypatch.setattr(env.settings, "memory_ask_deep_select_tier", "cheap")
    monkeypatch.setattr(env.settings, "memory_ask_mid_model", "claude-sonnet-5")
    return env


@pytest.mark.asyncio
async def test_a_temporal_ask_on_the_mid_tier_takes_the_deep_first_order(deep_env) -> None:
    """The first synthesis is skipped under deep-first; the verifier must not
    read that empty placeholder as a failed mid answer and escalate it as
    `mid_empty`."""
    out = await _ask(query="when did she meet Samantha", decompose="off")
    assert out.route_tier == "mid" and out.route_reason == "weak_top_hit:mid"
    assert not out.escalated and out.synth_model == "claude-sonnet-5"
    assert out.deep_first == "single" and out.depth == "raw"
    assert deep_env.mid.raw_calls == 1 and deep_env.mid.calls == 1
    assert deep_env.strong.calls == 0


@pytest.mark.asyncio
async def test_a_failed_mid_deep_answer_escalates_once_over_the_raw_block(deep_env) -> None:
    deep_env.mid.deep_text = "I don't know."  # deep answer hedges + uncited
    deep_env.mid.text = "I don't know."  # so does the excerpt fallback
    out = await _ask(query="when did she meet Samantha", decompose="off")
    assert out.route_tier == "strong" and out.escalated
    assert out.route_reason == "weak_top_hit:mid:mid_uncited"
    assert out.synth_model == "strong" and out.depth == "raw"
    # exactly one strong call, and it read the raw block
    assert deep_env.strong.calls == 1 and deep_env.strong.raw_calls == 1
