"""Probe proposed providers without changing the running configuration.

Checks make small billable requests. Call only after an operator explicitly
asks to validate. Never return upstream response bodies or exception strings:
they may contain credentials or request headers.
"""

import asyncio
import math
from collections.abc import Mapping
from typing import Literal

import httpx

from server.core.llm_provider import provider_for_model
from server.embedding.compatible import CompatibleEmbedder
from server.embedding.gemini import GeminiEmbedder
from server.embedding.openai import OpenAIEmbedder
from server.insights.api.models_provider_setup import (
    ProviderCheck,
    ProviderSetupInput,
    ProviderValidation,
)
from server.llm.anthropic import AnthropicLLMClient
from server.llm.openai import OpenAILLMClient
from server.observability.genai import llm_stage


async def _probe_llm(provider: str, model: str, key: str) -> None:
    client = (
        AnthropicLLMClient(model, api_key=key)
        if provider == "anthropic"
        else OpenAILLMClient(model, api_key=key)
    )
    with llm_stage("provider_validation"):
        result = await client.complete(
            "This is a connection check. Reply with OK.", "Connection check.", max_tokens=128
        )
    if not result.text.strip():
        raise ValueError("empty completion")


async def _probe_embedding(
    provider: str,
    model: str,
    key: str,
    dimensions: int,
    base_url: str = "",
    send_dimensions: bool = False,
) -> None:
    client = (
        CompatibleEmbedder(
            model=model,
            dim=dimensions,
            api_key=key,
            base_url=base_url,
            send_dimensions=send_dimensions,
        )
        if provider == "compatible"
        else (
            OpenAIEmbedder(model=model, dim=dimensions, api_key=key)
            if provider == "openai"
            else GeminiEmbedder(model=model, dim=dimensions, api_key=key)
        )
    )
    # Bypass the vector cache: a cache hit would falsely validate an invalid key.
    # Also do not insert a validation-only vector into any data-plane table.
    vector = await client._embed_one_raw("Loom provider connection check.")
    if len(vector) != dimensions or not all(math.isfinite(v) for v in vector):
        raise ValueError("invalid embedding dimensions or values")
    if not any(vector):
        raise ValueError("zero embedding")


async def _check(
    role: Literal["llm", "embedding"],
    provider: str,
    model: str,
    key: str,
    dimensions: int,
    base_url: str = "",
    send_dimensions: bool = False,
) -> ProviderCheck:
    check = ProviderCheck(
        role=role, provider=provider, model=model, ready=False, code="provider_error"
    )
    if provider not in ("openai", "anthropic", "gemini", "compatible"):
        check.code = "unsupported_provider"
        return check
    if not key.strip():
        check.code = "missing_key"
        return check
    try:
        async with asyncio.timeout(30):
            if role == "llm":
                await _probe_llm(provider, model, key)
            else:
                await _probe_embedding(provider, model, key, dimensions, base_url, send_dimensions)
    except (TimeoutError, httpx.TimeoutException):
        check.code = "timeout"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            check.code = "invalid_credentials"
        elif exc.response.status_code == 404:
            check.code = "model_unavailable"
        elif exc.response.status_code == 429:
            check.code = "quota_or_rate_limit"
        else:
            check.code = "provider_error"
    except (ValueError, KeyError, IndexError, TypeError):
        check.code = "invalid_output"
    except Exception:  # noqa: BLE001 — isolate probe failure; never expose upstream secret-bearing errors
        check.code = "provider_error"
    else:
        check.ready = True
        check.code = "ready"
    return check


async def validate_providers(
    setup: ProviderSetupInput, existing_keys: Mapping[str, str]
) -> ProviderValidation:
    """Check every distinct model, using submitted keys ahead of stored keys.

    Missing credentials never fall back to fake providers. The caller owns access
    control and secret loading; this function neither persists nor activates keys.
    """
    keys = dict(existing_keys)
    keys.update({name: value.get_secret_value() for name, value in setup.keys.items()})
    models: set[tuple[str, str]] = {(setup.llm.provider, setup.llm.model)}
    extra = list(setup.llm.overrides.values())
    extra.extend(setup.llm.verification_models or [])
    models.update((provider_for_model(model, setup.llm.provider), model) for model in extra)
    checks = [
        await _check("llm", provider, model, keys.get(provider, ""), 0)
        for provider, model in sorted(models)
    ]
    embedding = setup.embedding
    checks.append(
        await _check(
            "embedding",
            embedding.provider,
            embedding.model,
            keys.get(embedding.provider, ""),
            embedding.dimensions,
            embedding.base_url,
            embedding.send_dimensions,
        )
    )
    return ProviderValidation(ready=all(check.ready for check in checks), checks=checks)
