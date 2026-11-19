"""Native provider contracts, without AWS credentials or network calls."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from server.core.llm_provider import provider_for_model, resolve_llm_provider
from server.embedding._provider import EmbedderInputTooLarge, MalformedEmbedding
from server.llm import customer
from server.observability import provider_health
from server.settings import Settings, settings


@pytest.fixture(autouse=True)
def isolated_calls(monkeypatch):
    monkeypatch.setattr(customer, "require", AsyncMock(return_value=customer.Customer()))
    provider_health.reset()
    yield
    provider_health.reset()


def test_native_model_names_route_without_vendor_api_keys():
    resolved = resolve_llm_provider("", "bedrock:us.anthropic.example-v1:0", {})
    assert (resolved.provider, resolved.model) == ("bedrock", "bedrock:us.anthropic.example-v1:0")
    assert provider_for_model("bedrock:profile", "openai") == "bedrock"
    assert resolve_llm_provider("", "", {}).provider == "fake"
    with pytest.raises(ValueError, match="LOOM_LLM_MODEL"):
        Settings(_env_file=None, llm_provider="bedrock", llm_model="")


def test_native_embedding_defaults_and_explicit_provider_are_respected():
    native = Settings(_env_file=None, embedding_model="bedrock:cohere.embed-v4:0")
    assert native.embedding_dim == 1536
    fake = Settings(
        _env_file=None, embedding_model="bedrock:cohere.embed-v4:0", embedding_provider="fake"
    )
    assert fake.embedding_dim == 3072
    with pytest.raises(ValueError, match="Cohere Embed v4"):
        Settings(_env_file=None, embedding_provider="bedrock")


def test_native_factories_do_not_fall_back_to_fake_without_api_keys(monkeypatch):
    from server.embedding.bedrock import BedrockEmbedder
    from server.embedding.embedder import _build, provider_for
    from server.llm.bedrock import BedrockLLMClient
    from server.llm.client import _build_llm_client

    monkeypatch.setattr(settings, "embedding_dim", 1536)
    assert provider_for("bedrock:cohere.embed-v4:0") == "bedrock"
    assert isinstance(_build("bedrock:cohere.embed-v4:0", "bedrock"), BedrockEmbedder)
    assert isinstance(
        _build_llm_client("bedrock", "bedrock:profile", fake_label="fake", warn=""),
        BedrockLLMClient,
    )


@pytest.mark.asyncio
async def test_converse_preserves_body_usage_and_stop_reason(monkeypatch):
    from server.clients import bedrock
    from server.llm.bedrock import BedrockLLMClient

    call = AsyncMock(
        return_value={
            "output": {"message": {"content": [{"reasoningContent": {}}, {"text": "hello"}]}},
            "usage": {
                "inputTokens": 12,
                "outputTokens": 7,
                "cacheReadInputTokens": 5,
                "cacheWriteInputTokens": 3,
            },
            "stopReason": "max_tokens",
        }
    )
    monkeypatch.setattr(bedrock, "call", call)
    result = await BedrockLLMClient("bedrock:us.anthropic.example-v1:0").complete(
        "instructions", "question", max_tokens=4000
    )
    operation, payload, who = call.call_args.args
    assert operation == "converse"
    assert payload["modelId"] == "us.anthropic.example-v1:0"
    assert payload["system"] == [{"text": "instructions"}]
    assert payload["messages"] == [{"role": "user", "content": [{"text": "question"}]}]
    assert payload["inferenceConfig"]["maxTokens"] == 4000
    assert isinstance(who, customer.Customer)
    assert result.text == "hello"
    assert (result.tokens_in, result.tokens_out) == (20, 7)
    assert (result.cached_input_tokens, result.cache_write_input_tokens) == (5, 3)
    assert (result.provider, result.stop_reason) == ("bedrock", "max_tokens")


@pytest.mark.asyncio
async def test_json_mode_and_optional_system(monkeypatch):
    from server.clients import bedrock
    from server.llm.bedrock import BedrockLLMClient

    call = AsyncMock(
        return_value={
            "output": {"message": {"content": [{"text": '```json\n{"ok":true}\n```'}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
            "stopReason": "end_turn",
        }
    )
    monkeypatch.setattr(bedrock, "call", call)
    client = BedrockLLMClient("model")
    result = await client.complete("", "question", response_format={"type": "json_object"})
    assert result.text == '{"ok":true}'
    assert "JSON" in call.call_args.args[1]["system"][0]["text"]
    await client.complete("", "question")
    assert "system" not in call.call_args.args[1]


@pytest.mark.asyncio
async def test_guardrail_without_text_preserves_refusal_without_provider_outage(monkeypatch):
    from server.clients import bedrock
    from server.llm.bedrock import BedrockLLMClient

    monkeypatch.setattr(
        bedrock,
        "call",
        AsyncMock(
            return_value={
                "stopReason": "guardrail_intervened",
                "usage": {"inputTokens": 3, "outputTokens": 0},
            }
        ),
    )
    result = await BedrockLLMClient("model").complete("", "question")
    assert result.text == ""
    assert result.stop_reason == "guardrail_intervened"
    assert not provider_health.status()["providers"]["bedrock:messages"]["failing"]


@pytest.mark.asyncio
async def test_cohere_query_and_documents_use_different_input_types(monkeypatch):
    from server.clients import bedrock
    from server.embedding.bedrock import BedrockEmbedder

    calls = []

    async def invoke(operation, payload, who, **kwargs):
        calls.append((operation, json.loads(payload["body"])))
        count = len(calls[-1][1]["texts"])
        return {"body": {"embeddings": [[1.0] * 256 for _ in range(count)]}}

    monkeypatch.setattr(bedrock, "call", invoke)
    embedder = BedrockEmbedder(model="bedrock:cohere.embed-v4:0", dim=256)
    assert len(await embedder.embed("where is it")) == 256
    assert len(await embedder.embed_many(["document one", "document two"])) == 2
    assert [payload["input_type"] for _, payload in calls] == ["search_query", "search_document"]
    assert all(op == "invoke_model" for op, _ in calls)
    assert all(payload["truncate"] == "NONE" for _, payload in calls)
    assert all(payload["output_dimension"] == 256 for _, payload in calls)


@pytest.mark.asyncio
async def test_cohere_batches_are_bounded_and_keep_order(monkeypatch):
    from server.clients import bedrock
    from server.embedding.bedrock import BedrockEmbedder

    sizes = []

    async def invoke(operation, payload, who, **kwargs):
        texts = json.loads(payload["body"])["texts"]
        sizes.append(len(texts))
        return {"body": {"embeddings": {"float": [[float(t)] * 256 for t in texts]}}}

    monkeypatch.setattr(bedrock, "call", invoke)
    result = await BedrockEmbedder(model="cohere.embed-v4:0", dim=256).embed_many(
        [str(i) for i in range(97)]
    )
    assert sizes == [96, 1]
    assert [v[0] for v in result] == list(range(97))


@pytest.mark.asyncio
async def test_cohere_batches_bound_serialized_utf8_json_without_changing_text(monkeypatch):
    from server.clients import bedrock
    from server.embedding import bedrock as module

    texts = ["é" * 32, "\n" * 16, "🙂" * 8]
    bodies = []
    limit = 230
    monkeypatch.setattr(module, "_MAX_REQUEST_BYTES", limit, raising=False)

    async def invoke(operation, payload, who, **kwargs):
        raw = payload["body"]
        body = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        assert len(body) <= limit
        bodies.append(body)
        values = json.loads(body)["texts"]
        return {"body": {"embeddings": [[float(texts.index(text))] * 256 for text in values]}}

    monkeypatch.setattr(bedrock, "call", invoke)
    vectors = await module.BedrockEmbedder(model="cohere.embed-v4:0", dim=256).embed_many(texts)
    assert len(bodies) == 2
    assert [text for body in bodies for text in json.loads(body)["texts"]] == texts
    assert [vec[0] for vec in vectors] == [0.0, 1.0, 2.0]
    assert any("é".encode() in body for body in bodies)


@pytest.mark.asyncio
async def test_cohere_oversized_later_item_is_refused_before_any_request(monkeypatch):
    from server.clients import bedrock
    from server.embedding import bedrock as module

    monkeypatch.setattr(module, "_MAX_REQUEST_BYTES", 230, raising=False)
    request = AsyncMock(return_value={"body": {"embeddings": [[1.0] * 256] * 2}})
    monkeypatch.setattr(bedrock, "call", request)
    with pytest.raises(EmbedderInputTooLarge):
        await module.BedrockEmbedder(model="cohere.embed-v4:0", dim=256).embed_many(
            ["ok", "x" * 231]
        )
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_document_batches_stay_under_actual_service_body_limit(monkeypatch):
    from server.clients import bedrock
    from server.embedding.bedrock import BedrockEmbedder

    counts = []

    async def invoke(operation, payload, who, **kwargs):
        body = payload["body"]
        assert isinstance(body, bytes)
        assert len(body) <= 20_000_000
        texts = json.loads(body)["texts"]
        counts.append(len(texts))
        return {"body": {"embeddings": [[1.0] * 256 for _ in texts]}}

    monkeypatch.setattr(bedrock, "call", invoke)
    result = await BedrockEmbedder(model="cohere.embed-v4:0", dim=256).embed_many(
        ["x" * 300_000] * 96
    )
    assert len(result) == sum(counts) == 96
    assert len(counts) == 2


@pytest.mark.asyncio
async def test_cohere_missing_usage_is_reported_as_unknown(monkeypatch):
    from server.clients import bedrock
    from server.embedding import bedrock as embed_module

    span = Mock()

    @contextmanager
    def observe(**kwargs):
        yield span

    monkeypatch.setattr(embed_module, "embedding_span", observe)
    monkeypatch.setattr(
        bedrock,
        "call",
        AsyncMock(
            return_value={
                "body": {"embeddings": [[1.0] * 256]},
            }
        ),
    )
    await embed_module.BedrockEmbedder(model="cohere.embed-v4:0", dim=256).embed_many(["document"])
    assert span.record_usage.call_args.kwargs["usage_known"] is False
    assert span.record_usage.call_args.kwargs["called_provider"] is True


@pytest.mark.parametrize(
    "raw,expected", [(None, None), (True, None), (3.5, None), ("7", 7), (0, 0)]
)
def test_only_reported_integral_embedding_usage_is_counted(raw, expected):
    from server.embedding.bedrock import _billed_tokens

    assert (
        _billed_tokens(
            "cohere.embed-v4:0",
            {
                "body": {},
                "ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": raw}},
            },
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("vectors", [[], [[1.0]], [[float("nan")] * 256]])
async def test_cohere_refuses_bad_vectors(monkeypatch, vectors):
    from server.clients import bedrock
    from server.embedding.bedrock import BedrockEmbedder

    monkeypatch.setattr(bedrock, "call", AsyncMock(return_value={"body": {"embeddings": vectors}}))
    with pytest.raises(MalformedEmbedding):
        await BedrockEmbedder(model="cohere.embed-v4:0", dim=256).embed_many(["document"])
    assert provider_health.status()["providers"]["bedrock:embeddings"]["failing"]


@pytest.mark.asyncio
async def test_provider_auth_failure_is_visible_and_recovery_clears_it(monkeypatch):
    from server.clients import bedrock
    from server.llm.bedrock import BedrockLLMClient

    request = httpx.Request("POST", "https://bedrock-runtime.example/model")
    error = httpx.HTTPStatusError(
        "refused", request=request, response=httpx.Response(403, request=request)
    )
    call = AsyncMock(side_effect=error)
    monkeypatch.setattr(bedrock, "call", call)
    client = BedrockLLMClient("model")
    with pytest.raises(httpx.HTTPStatusError):
        await client.complete("", "question")
    assert provider_health.status()["providers"]["bedrock:messages"]["failing"]
    call.side_effect = None
    call.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": {}}
    await client.complete("", "question")
    assert not provider_health.status()["providers"]["bedrock:messages"]["failing"]


@pytest.mark.asyncio
async def test_required_gateway_refuses_native_before_sdk_call(monkeypatch):
    from server.clients import bedrock
    from server.core.errors import GatewayRequiredError

    monkeypatch.setattr(settings, "inference_gateway_required", True)
    sdk = AsyncMock()
    monkeypatch.setattr(bedrock.asyncio, "to_thread", sdk)
    with pytest.raises(GatewayRequiredError):
        await bedrock.call(
            "converse", {"modelId": "model"}, customer.Customer(), request_timeout_s=5.0, attempts=1
        )
    sdk.assert_not_called()


@pytest.mark.asyncio
async def test_model_call_refusal_reaches_caller_without_marking_provider(monkeypatch):
    from server.llm.bedrock import BedrockLLMClient

    monkeypatch.setattr(
        customer, "require", AsyncMock(side_effect=customer.ModelCallsDisabled("disabled"))
    )
    with pytest.raises(customer.ModelCallsDisabled):
        await BedrockLLMClient("model").complete("", "question")
    assert not provider_health.status()["providers"]
