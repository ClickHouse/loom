"""Configuration refuses before spend reads; routed aliases retain explicit prices."""

import asyncio
import importlib.util
from unittest.mock import AsyncMock

import pytest

from server.assistant.app import agent, runtime, service
from server.core.errors import AssistantUnavailableError
from server.llm import agent as models
from server.observability import cost_accumulator
from server.settings import Settings, settings


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "assistant_enabled", True)
    monkeypatch.setattr(settings, "assistant_provider", "anthropic")
    monkeypatch.setattr(settings, "assistant_model", "claude-haiku-4-5")
    monkeypatch.setattr(settings, "assistant_price_model", "")
    monkeypatch.setattr(settings, "assistant_answer_ceiling_usd", 0.03)
    monkeypatch.setattr(settings, "assistant_max_output_tokens", 128)
    monkeypatch.setattr(settings, "assistant_max_context_bytes", 12000)
    monkeypatch.setattr(settings, "assistant_max_tokens", 14000)
    monkeypatch.setattr(settings, "bedrock_region", "us-east-1")
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda _: True)
    monkeypatch.setattr(type(settings), "gateway_required", lambda _: False)
    monkeypatch.setattr(service.gateway, "keyless", lambda: False)
    monkeypatch.setattr(service, "provider_key", lambda _: "fixture-key")
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        service.importlib.util,
        "find_spec",
        lambda name: object() if name == "botocore" else find_spec(name),
    )


@pytest.mark.parametrize("model", ("gemini-3.1-flash-lite", "vertex:gemini-3.1-flash-lite"))
def test_vertex_assistant_uses_its_own_provider_and_a_priced_model(configured, monkeypatch, model):
    loaded = Settings(
        _env_file=None,
        llm_provider="fake",
        assistant_enabled=True,
        assistant_provider="vertex",
        assistant_model=model,
        assistant_answer_ceiling_usd=0.03,
        vertex_project="test-project",
        vertex_location="global",
    )
    monkeypatch.setattr(service, "settings", loaded)
    assert loaded.llm_provider == "fake"
    assert service.assistant_state() == (True, "")
    definition = service.current_definition()
    assert definition.model == definition.price_model == "vertex:gemini-3.1-flash-lite"
    assert cost_accumulator.turn_cost_usd(
        definition.price_model, input_tokens=1000, cache_read_tokens=1000, output_tokens=1000
    ) == pytest.approx(0.000275 + 0.0000275 + 0.00165)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("assistant_model", "", "LOOM_ASSISTANT_MODEL"),
        ("assistant_model", "claude-sonnet", "Gemini model ID"),
        ("vertex_project", "", "LOOM_VERTEX_PROJECT and LOOM_VERTEX_LOCATION"),
        ("vertex_project", "https://example.com", "LOOM_VERTEX_PROJECT must name"),
        ("vertex_location", "", "LOOM_VERTEX_PROJECT and LOOM_VERTEX_LOCATION"),
        ("vertex_location", "europe-west4/inject", "LOOM_VERTEX_LOCATION must be"),
    ],
)
def test_vertex_assistant_validates_its_route_at_settings_load(field, value, error):
    route = {
        "assistant_model": "gemini-3.1-flash-lite",
        "vertex_project": "test-project",
        "vertex_location": "global",
    }
    route[field] = value
    with pytest.raises(ValueError, match=error):
        Settings(_env_file=None, llm_provider="fake", assistant_provider="vertex", **route)


@pytest.mark.parametrize("policy", ("required", "keyless"))
def test_vertex_assistant_requires_a_direct_route(configured, monkeypatch, policy):
    monkeypatch.setattr(settings, "assistant_provider", "vertex")
    monkeypatch.setattr(settings, "assistant_model", "vertex:gemini-3.1-flash-lite")
    monkeypatch.setattr(type(settings), "gateway_required", lambda _: policy == "required")
    monkeypatch.setattr(service.gateway, "keyless", lambda: policy == "keyless")
    assert service.assistant_state() == (False, "vertex_requires_direct_route")


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("disabled", "not_configured"),
        ("auth_off", "auth_off"),
        ("legacy_enablement", "provider_required"),
        ("model_missing", "model_required"),
        ("anthropic_model_missing", "model_required"),
        ("budget_zero", "budget_required"),
        ("model_unpriced", "model_price_unknown"),
        ("alias_unpriced", "model_price_unknown"),
        ("embedding_only_price", "model_price_unknown"),
        ("real_provider_free_price", "model_price_unknown"),
        ("key_missing", "provider_key_missing"),
        ("bedrock_region_missing", "bedrock_configuration_missing"),
        ("bedrock_signer_missing", "bedrock_configuration_missing"),
        ("bedrock_gateway_required", "bedrock_requires_direct_route"),
        ("bedrock_keyless_gateway", "bedrock_requires_direct_route"),
    ],
)
def test_configuration_refuses_before_admission_reads(configured, monkeypatch, condition, reason):
    if condition.startswith("bedrock_"):
        monkeypatch.setattr(settings, "assistant_provider", "bedrock")
    if condition == "disabled":
        monkeypatch.setattr(settings, "assistant_enabled", False)
    elif condition == "auth_off":
        monkeypatch.setattr(type(settings), "auth_actually_required", lambda _: False)
    elif condition == "legacy_enablement":
        monkeypatch.setattr(settings, "assistant_provider", "")
        monkeypatch.setattr(settings, "assistant_model", "")
    elif condition == "model_missing":
        monkeypatch.setattr(settings, "assistant_provider", "openai")
        monkeypatch.setattr(settings, "assistant_model", "")
    elif condition == "anthropic_model_missing":
        monkeypatch.setattr(settings, "assistant_model", "")
    elif condition == "budget_zero":
        monkeypatch.setattr(settings, "assistant_answer_ceiling_usd", 0)
    elif condition == "model_unpriced":
        monkeypatch.setattr(settings, "assistant_model", "unpriced-route")
    elif condition == "alias_unpriced":
        monkeypatch.setattr(settings, "assistant_price_model", "unpriced-alias")
    elif condition == "embedding_only_price":
        monkeypatch.setattr(settings, "assistant_price_model", "text-embedding-3-small")
    elif condition == "real_provider_free_price":
        monkeypatch.setattr(settings, "assistant_price_model", "fake-echo-1")
    elif condition == "key_missing":
        monkeypatch.setattr(service, "provider_key", lambda _: "")
    elif condition == "bedrock_region_missing":
        monkeypatch.setattr(settings, "bedrock_region", "")
    elif condition == "bedrock_signer_missing":
        find_spec = importlib.util.find_spec
        monkeypatch.setattr(
            service.importlib.util,
            "find_spec",
            lambda name: None if name == "botocore" else find_spec(name),
        )
    elif condition == "bedrock_gateway_required":
        monkeypatch.setattr(type(settings), "gateway_required", lambda _: True)
    elif condition == "bedrock_keyless_gateway":
        monkeypatch.setattr(service.gateway, "keyless", lambda: True)
    assert service.configuration_error() == reason
    assert service.assistant_state() == ((None, "") if condition == "disabled" else (False, reason))
    org_budget = AsyncMock(
        side_effect=AssertionError("configuration must refuse before spend reads")
    )
    monkeypatch.setattr(runtime, "switched_off", org_budget)
    with pytest.raises(AssistantUnavailableError):
        asyncio.run(runtime._afford("example", "personal"))
    org_budget.assert_not_awaited()


def test_routed_alias_reserves_and_accounts_at_its_explicit_price(configured, monkeypatch):
    monkeypatch.setattr(settings, "assistant_model", "routed-model-alias")
    monkeypatch.setattr(settings, "assistant_price_model", "claude-haiku-4-5")
    assert service.configuration_error() == ""
    requested = []

    class Replay:
        async def stream(self, messages, descriptors, maximum, turn):
            assert maximum == 128 and descriptors == []
            turn.sent = turn.usage_complete = True
            turn.usage = models.Usage(100, 10)
            turn.stop, turn.text = "stop", "Hello!"
            yield turn.text

    def model_for(provider, model):
        requested.append((provider, model))
        return Replay()

    monkeypatch.setattr(models, "model_for", model_for)

    async def scenario():
        acc = cost_accumulator.begin_op()
        state, turn = agent.State(), models.Turn()
        frames = [
            frame
            async for frame in agent._model(
                state, [{"role": "user", "content": "Hello!"}], [], turn
            )
        ]
        expected = cost_accumulator.turn_cost_usd(
            "claude-haiku-4-5", input_tokens=100, output_tokens=10
        )
        assert requested == [("anthropic", "routed-model-alias")]
        assert frames[0]["model"] == "routed-model-alias"
        assert state.turns == 1 and state.spent == pytest.approx(expected)
        assert acc.llm_calls == 1 and acc.cost_usd == pytest.approx(expected)
        assert not acc.cost_estimated

    asyncio.run(scenario())
