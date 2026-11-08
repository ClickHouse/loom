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

from server.clients._http_errors import provider_error_code
from server.core.errors import GatewayRequiredError
from server.core.llm_provider import provider_for_model
from server.core.schema.vector_codec_ddl import codable
from server.core.vectors import codes_state
from server.embedding._provider import MalformedEmbedding
from server.embedding.compatible import CompatibleEmbedder
from server.embedding.gemini import GeminiEmbedder
from server.embedding.local import LocalEmbedder
from server.embedding.openai import OpenAIEmbedder
from server.insights.api.models_provider_setup import (
    ProviderCheck,
    ProviderSetupInput,
    ProviderValidation,
)
from server.llm import customer, gateway
from server.llm.anthropic import AnthropicLLMClient
from server.llm.openai import OpenAILLMClient
from server.observability import provider_health
from server.observability.genai import llm_stage
from server.settings import settings
from server.settings.budgets import llm_max_tokens


async def _probe_llm(provider: str, model: str, key: str) -> None:
    client = (
        AnthropicLLMClient(model, api_key=key)
        if provider == "anthropic"
        else OpenAILLMClient(model, api_key=key)
    )
    with llm_stage("provider_validation"), customer.system():
        result = await client.complete(
            "This is a connection check. Reply with OK.",
            "Connection check.",
            max_tokens=llm_max_tokens("provider_validation"),
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
    if provider == "local":
        # Loads the model the server would, fetching a pinned file it lacks.
        vector = await LocalEmbedder(spec=model).embed("Loom provider connection check.")
        if len(vector) != dimensions or not all(math.isfinite(v) for v in vector):
            raise ValueError("invalid embedding dimensions or values")
        return
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
    with customer.system():
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
    if provider not in ("openai", "anthropic", "gemini", "compatible", "local"):
        check.code = "unsupported_provider"
        return check
    keyless = provider == "local" or (
        provider in ("openai", "anthropic", "compatible") and gateway.keyless()
    )
    if not key.strip() and not keyless:
        check.code = "missing_key"
        return check
    try:
        with provider_health.unrecorded():
            async with asyncio.timeout(settings.provider_check_timeout_s):
                if role == "llm":
                    await _probe_llm(provider, model, key)
                else:
                    await _probe_embedding(
                        provider, model, key, dimensions, base_url, send_dimensions
                    )
    except GatewayRequiredError:
        check.code = "gateway_required"
    except (TimeoutError, httpx.TimeoutException):
        check.code = "timeout"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        # The body's error code only: its message can quote part of the key.
        code = provider_error_code(exc)
        check.detail = f"HTTP {status} ({code})" if code else f"HTTP {status}"
        if status in (401, 403):
            check.code = "invalid_credentials"
        elif status == 404:
            check.code = "model_unavailable"
        elif status in (402, 429):
            check.code = "quota_or_rate_limit"
        else:
            check.code = "provider_error"
    except MalformedEmbedding as exc:
        # Loom's own wording (width, empty, non-finite), never the response body.
        check.code = "invalid_output"
        check.detail = str(exc)
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
    if await codes_state.served_model_writes_coded() and not codable(embedding.dimensions):
        # The rebuild re-creates the coded vector tables at the new width.
        checks.append(
            ProviderCheck(
                role="embedding",
                provider=embedding.provider,
                model=embedding.model,
                ready=False,
                code="unsupported_dimensions",
                detail=f"{embedding.dimensions} is not a multiple of 8",
            )
        )
    else:
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
