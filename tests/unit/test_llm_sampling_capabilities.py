"""Known sampling restrictions apply before any provider request."""

from unittest.mock import AsyncMock

import httpx
import pytest

from server.clients import bedrock
from server.llm import anthropic, customer, gateway, openai, traits
from server.llm.bedrock import BedrockLLMClient

MODELS_WITHOUT_TEMPERATURE = (
    "gpt-6-luna",
    "claude-sonnet-5-5",
    "claude-sonnet-5-5-20260928",
    "us.anthropic.claude-sonnet-5-5",
    "bedrock:eu.anthropic.claude-sonnet-5-5:0",
    "global.anthropic.claude-opus-4-7-v1:0",
    "anthropic.claude-opus-4-8",
    "claude-opus-5",
    "claude-opus-5-5",
    "claude-sonnet-5",
    "claude-haiku-5-5",
    "arn:aws:bedrock:us-east-2:123456789012:inference-profile/us.anthropic.claude-sonnet-5-5",
)


@pytest.fixture(autouse=True)
def fresh_traits():
    traits.reset_for_tests()
    yield
    traits.reset_for_tests()


@pytest.mark.parametrize("model", MODELS_WITHOUT_TEMPERATURE)
@pytest.mark.parametrize("temperature", [0, 0.7, None])
def test_known_models_omit_temperature_in_both_http_protocols(model, temperature):
    for _ in range(2):
        traits.reset_for_tests()
        for payload in (
            openai._payload(model, "instructions", "question", 1024, None, temperature),
            anthropic._payload(model, "instructions", "question", 1024, temperature),
        ):
            assert "temperature" not in payload
            assert payload["model"] == model
            assert payload.get("max_tokens", payload.get("max_completion_tokens")) == 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [openai, anthropic])
@pytest.mark.parametrize("model", ["gpt-6-luna", "claude-sonnet-5-5"])
async def test_http_client_sends_one_valid_request_from_a_cold_process(monkeypatch, module, model):
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://provider.example"),
        json={
            "choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}],
            "content": [{"type": "text", "text": "complete"}],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 1,
                "input_tokens": 4,
                "output_tokens": 1,
            },
        },
    )
    http = AsyncMock()
    http.post.return_value = response
    monkeypatch.setattr(module, "_http_client", lambda: http)
    monkeypatch.setattr(gateway, "bearer", AsyncMock(return_value="test-key"))
    monkeypatch.setattr(customer, "require", AsyncMock(return_value=customer.Customer()))
    client_class = openai.OpenAILLMClient if module is openai else anthropic.AnthropicLLMClient
    for _ in range(2):
        traits.reset_for_tests()
        result = await client_class(model, api_key="test-key").complete("", "question")
        assert result.text == "complete"
        assert "temperature" not in http.post.call_args.kwargs["json"]
    assert http.post.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS_WITHOUT_TEMPERATURE)
async def test_known_bedrock_model_never_sends_temperature_even_after_restart(monkeypatch, model):
    call = AsyncMock(
        return_value={
            "output": {"message": {"content": [{"text": "complete"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 4, "outputTokens": 1},
        }
    )
    monkeypatch.setattr(bedrock, "call", call)
    monkeypatch.setattr(customer, "require", AsyncMock(return_value=customer.Customer()))
    for _ in range(2):
        traits.reset_for_tests()
        result = await BedrockLLMClient(model).complete("instructions", "question", temperature=0)
        assert result.text == "complete"
        assert call.call_args.args[1]["inferenceConfig"] == {"maxTokens": 1024}
    assert call.await_count == 2


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929",
        "us.anthropic.claude-opus-4-6-v1:0",
        "claude-sonnet-5-50",
        "other-claude-sonnet-5-5",
        "arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/opaque",
        "opaque-gateway-alias",
    ],
)
def test_other_models_keep_requested_temperature(model):
    assert openai._payload(model, "", "question", 1024, None, 0.7)["temperature"] == 0.7
    assert anthropic._payload(model, "", "question", 1024, 0.7)["temperature"] == 0.7


def test_learned_refusals_do_not_modify_other_models_or_documented_defaults():
    traits.traits("claude-sonnet-5-5").refused.add("output_config")
    assert traits.traits("us.anthropic.claude-sonnet-5-5").refused == {"temperature"}
    assert traits.traits("opaque-gateway-alias").refused == set()
    traits.reset_for_tests()
    assert traits.traits("claude-sonnet-5-5").refused == {"temperature"}
