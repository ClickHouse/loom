"""Model onboarding validates credentials without activating them."""

import os
from unittest.mock import AsyncMock

import httpx
import pytest

from server.insights.api.models_provider_setup import ProviderSetupInput
from server.insights.app import provider_validation as validation


def setup_input(**changes: object) -> ProviderSetupInput:
    body: dict[str, object] = {
        "llm": {"provider": "anthropic", "model": "claude-test"},
        "embedding": {"provider": "openai", "model": "text-embedding-test"},
    }
    body.update(changes)
    return ProviderSetupInput.model_validate(body)


@pytest.mark.asyncio
async def test_missing_keys_never_call_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, embed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(validation, "_probe_llm", llm)
    monkeypatch.setattr(validation, "_probe_embedding", embed)
    result = await validation.validate_providers(setup_input(), {})
    assert not result.ready
    assert [c.code for c in result.checks] == ["missing_key", "missing_key"]
    llm.assert_not_called()
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_keys_are_used_without_mutating_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, embed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(validation, "_probe_llm", llm)
    monkeypatch.setattr(validation, "_probe_embedding", embed)
    before = dict(os.environ)
    result = await validation.validate_providers(
        setup_input(keys={"anthropic": "new-key"}), {"anthropic": "old-key", "openai": "embed-key"}
    )
    assert result.ready
    llm.assert_awaited_once_with("anthropic", "claude-test", "new-key")
    embed.assert_awaited_once_with("openai", "text-embedding-test", "embed-key", 3072, "", False)
    assert dict(os.environ) == before
    assert "new-key" not in result.model_dump_json()
    assert "embed-key" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_compatible_probe_uses_its_endpoint_and_separate_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm, embed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(validation, "_probe_llm", llm)
    monkeypatch.setattr(validation, "_probe_embedding", embed)
    setup = setup_input(
        embedding={
            "provider": "compatible",
            "model": "custom-model",
            "dimensions": 4096,
            "base_url": "https://embedding.example/v1",
            "send_dimensions": True,
        }
    )
    result = await validation.validate_providers(
        setup, {"anthropic": "llm-key", "compatible": "embed-key"}
    )
    assert result.ready
    embed.assert_awaited_once_with(
        "compatible", "custom-model", "embed-key", 4096, "https://embedding.example/v1", True
    )
    assert "embed-key" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_advanced_models_require_their_own_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, embed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(validation, "_probe_llm", llm)
    monkeypatch.setattr(validation, "_probe_embedding", embed)
    setup = setup_input(
        llm={
            "provider": "anthropic",
            "model": "claude-test",
            "overrides": {"code_ask_llm_model": "gpt-other"},
            "verification_models": ["gpt-other", "claude-test"],
        },
        embedding={"provider": "gemini", "model": "gemini-embedding-001"},
    )
    result = await validation.validate_providers(setup, {"anthropic": "key", "gemini": "key"})
    assert not result.ready
    assert len(result.checks) == 3
    assert next(c for c in result.checks if c.model == "gpt-other").code == "missing_key"
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_openai_key_validates_both_roles_without_anthropic(monkeypatch):
    llm, embed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(validation, "_probe_llm", llm)
    monkeypatch.setattr(validation, "_probe_embedding", embed)
    config = setup_input(
        llm={"provider": "openai", "model": "gpt-test"}, keys={"openai": "one-test-key"}
    )
    result = await validation.validate_providers(config, {})
    assert result.ready
    assert {check.provider for check in result.checks} == {"openai"}
    llm.assert_awaited_once_with("openai", "gpt-test", "one-test-key")
    embed.assert_awaited_once_with("openai", "text-embedding-test", "one-test-key", 3072, "", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "invalid_credentials"),
        (403, "invalid_credentials"),
        (404, "model_unavailable"),
        (402, "quota_or_rate_limit"),
        (429, "quota_or_rate_limit"),
        (500, "provider_error"),
    ],
)
async def test_provider_errors_do_not_expose_response(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    error = httpx.HTTPStatusError(
        "secret-key in upstream error", request=response.request, response=response
    )
    monkeypatch.setattr(validation, "_probe_llm", AsyncMock(side_effect=error))
    result = await validation._check("llm", "openai", "gpt-test", "secret-key", 0)
    assert not result.ready
    assert result.code == code
    assert "secret-key" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_detail_names_status_and_error_code_without_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"error": {"code": "insufficient_quota", "message": "key secret-key has no credit"}}
    response = httpx.Response(402, json=body, request=httpx.Request("POST", "https://example.test"))
    error = httpx.HTTPStatusError("upstream", request=response.request, response=response)
    monkeypatch.setattr(validation, "_probe_llm", AsyncMock(side_effect=error))
    result = await validation._check("llm", "openai", "gpt-test", "secret-key", 0)
    assert result.detail == "HTTP 402 (insufficient_quota)"
    assert "secret-key" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_width_mismatch_says_expected_and_returned_dims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validation.OpenAIEmbedder,
        "_embed_one_raw",
        AsyncMock(
            side_effect=validation.MalformedEmbedding(
                "provider returned 1024 dims; the request asked for 768"
            )
        ),
    )
    result = await validation._check("embedding", "openai", "text-embedding-test", "key", 768)
    assert result.code == "invalid_output"
    assert result.detail == "provider returned 1024 dims; the request asked for 768"


@pytest.mark.asyncio
async def test_failed_check_leaves_running_provider_health_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import provider_health

    async def failing(*_: object) -> None:
        with provider_health.recording("openai:chat"):
            raise RuntimeError("candidate key rejected")

    provider_health.reset()
    monkeypatch.setattr(validation, "_probe_llm", failing)
    result = await validation._check("llm", "openai", "gpt-test", "key", 0)
    assert result.code == "provider_error"
    assert provider_health.status() == {"degraded": False, "providers": {}}
    with pytest.raises(RuntimeError):
        await failing()
    assert provider_health.status()["degraded"]
    provider_health.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), httpx.ReadTimeout("secret-key")])
async def test_timeout_is_actionable(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setattr(validation, "_probe_llm", AsyncMock(side_effect=error))
    result = await validation._check("llm", "openai", "gpt-test", "secret-key", 0)
    assert result.code == "timeout"


@pytest.mark.asyncio
async def test_fake_override_is_never_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = AsyncMock()
    monkeypatch.setattr(validation, "_probe_llm", llm)
    result = await validation._check("llm", "fake", "fake-echo-1", "key", 0)
    assert result.code == "unsupported_provider"
    llm.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("vector", [[1.0], [float("nan"), 1.0], [0.0, 0.0]])
async def test_bad_vectors_are_rejected(
    monkeypatch: pytest.MonkeyPatch, vector: list[float]
) -> None:
    raw = AsyncMock(return_value=vector)
    monkeypatch.setattr(validation.OpenAIEmbedder, "_embed_one_raw", raw)
    result = await validation._check("embedding", "openai", "text-embedding-test", "key", 2)
    assert result.code == "invalid_output"
    raw.assert_awaited_once()


@pytest.mark.asyncio
async def test_embedding_probe_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = AsyncMock(return_value=[1.0, 0.0])
    cached = AsyncMock(side_effect=AssertionError("must not use cached path"))
    monkeypatch.setattr(validation.OpenAIEmbedder, "_embed_one_raw", raw)
    monkeypatch.setattr(validation.OpenAIEmbedder, "embed", cached)
    result = await validation._check("embedding", "openai", "text-embedding-test", "key", 2)
    assert result.ready
    raw.assert_awaited_once()
    cached.assert_not_called()


@pytest.mark.parametrize(
    "factory",
    [
        validation.OpenAILLMClient,
        validation.AnthropicLLMClient,
        validation.OpenAIEmbedder,
        validation.GeminiEmbedder,
    ],
)
def test_explicit_empty_key_does_not_use_environment(
    monkeypatch: pytest.MonkeyPatch, factory: object
) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "existing-key")
    with pytest.raises(RuntimeError):
        factory(model="test-model", api_key="")  # type: ignore[operator]


@pytest.mark.asyncio
@pytest.mark.parametrize(("coded", "ready"), [(True, False), (False, True)])
async def test_a_width_the_coded_tables_cannot_take_is_refused_before_any_probe(
    monkeypatch: pytest.MonkeyPatch, coded: bool, ready: bool
) -> None:
    from server.core.vectors import codes_state

    llm, embed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(validation, "_probe_llm", llm)
    monkeypatch.setattr(validation, "_probe_embedding", embed)
    monkeypatch.setattr(codes_state, "served_model_writes_coded", AsyncMock(return_value=coded))
    setup = setup_input(embedding={"provider": "openai", "model": "e", "dimensions": 12})
    result = await validation.validate_providers(setup, {"anthropic": "a", "openai": "o"})
    assert result.ready is ready
    embedding = result.checks[-1]
    assert embedding.code == ("ready" if ready else "unsupported_dimensions")
    assert embed.await_count == (1 if ready else 0)


@pytest.mark.asyncio
async def test_a_local_embedder_needs_no_key_and_is_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, embed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(validation, "_probe_llm", llm)
    monkeypatch.setattr(validation, "_probe_embedding", embed)
    local = {"provider": "local", "model": "local", "dimensions": 384}
    result = await validation.validate_providers(
        setup_input(embedding=local), {"anthropic": "llm-key"}
    )
    assert result.ready, result.checks
    embed.assert_awaited_once_with("local", "local", "", 384, "", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "ok"), [(384, True), (768, False)])
async def test_the_local_probe_embeds_with_the_named_model(
    monkeypatch: pytest.MonkeyPatch, width: int, ok: bool
) -> None:
    seen: list[str] = []

    class Local:
        def __init__(self, spec: str) -> None:
            seen.append(spec)

        async def embed(self, text: str) -> list[float]:
            return [0.1] * width

    monkeypatch.setattr(validation, "LocalEmbedder", Local)
    probe = validation._probe_embedding("local", "local:minilm", "", 384)
    if ok:
        await probe
    else:
        with pytest.raises(ValueError, match="dimensions"):
            await probe
    assert seen == ["local:minilm"]
