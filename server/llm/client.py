"""LLM client protocol + selector. Provider is resolved from settings at
config time, not in caller code (mirrors `server.embedding.embedder`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from server.core.llm_provider import KEY_ENV_BY_PROVIDER, provider_for_model


@dataclass(frozen=True)
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    provider: str
    model: str
    # Prompt tokens the provider served from its prefix cache (a subset of
    # tokens_in, billed at a discount). 0 when the provider reports none.
    cached_input_tokens: int = 0
    # Prompt tokens the provider WROTE to its prefix cache on this call (a
    # subset of tokens_in, billed at a premium on Anthropic). 0 when unreported.
    cache_write_input_tokens: int = 0
    # True when the exact-prompt response cache served this result without a
    # provider call (server/llm/cache.py). tokens_in/out are the
    # counts the original call reported; nothing was spent for this one.
    from_cache: bool = False
    # Why the provider ended the reply, as it says it (OpenAI `finish_reason`,
    # Anthropic `stop_reason`). "content_filter" / "refusal" mean the provider
    # cut the reply, so its text is partial. "" when unreported.
    stop_reason: str = ""


class LLMClient(Protocol):
    """Contract for any chat-completion provider used for synthesis."""

    provider: str
    model: str

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        """One-shot completion, no streaming or tools. `response_format`
        passes through where supported (OpenAI `{"type": "json_object"}`);
        providers that don't support it ignore the hint."""
        ...


def log_llm_provider_resolution() -> None:
    """The one boot INFO line saying what the process runs
    extraction on and what decided it. Silent when the operator set
    `LOOM_LLM_PROVIDER` — there is nothing inferred to report."""
    import logging

    from server.settings import settings

    if not settings.llm_provider_inferred():
        return
    provider, model = settings.llm_provider, settings.llm_model
    key_hint = ""
    if provider_key_missing(provider):
        key_hint = (
            f"; {KEY_ENV_BY_PROVIDER[provider]} is NOT set, so calls fall back to the"
            " fake LLM — set the key, or LOOM_LLM_PROVIDER=fake to make this explicit"
        )
    logging.getLogger(__name__).info(
        "LOOM_LLM_PROVIDER unset — inferred llm_provider=%s llm_model=%s from %s%s",
        provider,
        model,
        settings.llm_provider_source(),
        key_hint,
    )


def _key_absent(provider: str) -> bool:
    from server.core.provider_keys import provider_key
    from server.llm import gateway

    env = KEY_ENV_BY_PROVIDER.get(provider)
    return bool(env) and not provider_key(provider) and not gateway.keyless()


def log_conflict_resolver_resolution() -> None:
    """One boot WARN when the conflict resolver is on but its model's key is missing:
    writes are marked and wait, unjudged, for the key."""
    import logging

    from server.settings import settings

    provider = settings.conflict_resolver_provider()
    if not settings.memory_conflict_resolver_enabled or not provider_key_missing(provider):
        return
    logging.getLogger(__name__).warning(
        "conflict resolver: its model %s runs on %s and %s is not set; writes are marked "
        "and judged once the key is set",
        settings.conflict_resolver_model,
        provider,
        KEY_ENV_BY_PROVIDER.get(provider, "its key"),
    )


def provider_key_missing(provider: str) -> bool:
    """True when a keyed provider's API key is absent — the condition under
    which `_build_llm_client` silently falls back to the fake client.
    Diagnostics (doctor, the dashboard's degraded banner) use this so the fallback is
    visible instead of silent."""
    return _key_absent(provider)


def llm_is_fake() -> bool:
    """True when completions run on the fake client: configured so, or a keyed
    provider's key is absent."""
    from server.settings import settings

    return settings.llm_provider == "fake" or provider_key_missing(settings.llm_provider)


_cached: LLMClient | None = None


def _build_llm_client(provider: str, model: str, *, fake_label: str, warn: str) -> LLMClient:
    """Construct a client for (provider, model). Shared by the default and
    per-model-override selectors so the provider/fake/error branch lives once.
    A keyed provider (openai / anthropic) with its API key missing falls back
    to the fake client (logging `warn`) under `fake_label` — keeps the OSS
    dev container booting keyless.
    """
    import logging

    if provider_key_missing(provider):
        logging.getLogger(__name__).warning(warn)
        from server.llm.fake import FakeLLMClient

        return FakeLLMClient(model=fake_label)
    if provider == "openai":
        from server.llm.openai import OpenAILLMClient

        return OpenAILLMClient(model=model)
    if provider == "anthropic":
        from server.llm.anthropic import AnthropicLLMClient

        return AnthropicLLMClient(model=model)
    if provider == "local":
        from server.llm.local import LocalLLMClient

        return LocalLLMClient(model=model)
    if provider == "fake":
        from server.llm.fake import FakeLLMClient

        return FakeLLMClient(model=model)
    raise ValueError(f"unknown llm provider: {provider!r}")


def get_llm_client() -> LLMClient:
    """Returns the LLM client configured by `settings.llm_provider` — which
    `Settings` has already resolved (explicit value, else
    inferred from the model name, else from the key present, else fake)."""
    # Import settings dynamically rather than binding it at module level.
    # Several integration tests reload `server.settings` to swap the Settings
    # singleton during an ASGI-in-process flow (test_auth.py); a module-level
    # binding would capture the OLD singleton and miss later monkeypatches.
    from server.settings import settings

    global _cached
    if _cached is None:
        client = _build_llm_client(
            settings.llm_provider,
            settings.llm_model,
            fake_label="fake-echo-1",
            warn=(
                f"LOOM_LLM_PROVIDER={settings.llm_provider} but its API key is "
                "not set; falling back to the fake LLM client. Set the key to "
                "enable real conversation extraction / entity tagging."
            ),
        )
        _cached = _with_fallback(client, settings.llm_model)
    return _cached


def reset() -> None:
    """Test hook: drop the cached client so settings changes take effect."""
    global _cached, _override_cache, _wrapped_cache
    _cached = None
    _override_cache = {}
    _wrapped_cache = {}


def _with_fallback(client: LLMClient, model: str) -> LLMClient:
    """Wrap `client` in a FallbackLLMClient when LOOM_LLM_FALLBACK_MODEL names
    a different model. The secondary is the plain (unwrapped) override client
    for that model, so a fallback never chains into another fallback."""
    from server.settings import settings

    fallback = settings.llm_fallback_model.strip()
    if not fallback or fallback == model:
        return client
    from server.llm.fallback import FallbackLLMClient

    return FallbackLLMClient(client, _override_client(fallback))


# Per-model override cache. Keyed by (provider, model). Separate from
# `_cached` because stage-specific overrides (rerank, query rewrite)
# coexist with the default LLM at runtime.
_override_cache: dict[tuple[str, str], LLMClient] = {}

# The override model can come from a request (`memory.ask`'s `mid_model`) or a
# namespace's rerank endpoint, so the two override caches are bounded: past
# this many distinct models a cache is dropped and refills on use (a client is
# cheap to build and holds no connection of its own).
_MAX_OVERRIDE_CLIENTS = 128


def _remember[K, V](cache: dict[K, V], key: K, value: V) -> V:
    if len(cache) >= _MAX_OVERRIDE_CLIENTS:
        cache.clear()
    cache[key] = value
    return value


def get_llm_client_for_model(model: str | None) -> LLMClient:
    """Return an LLM client for a stage-specific model override.

    Empty/None `model` returns the default client. Otherwise returns a
    separately-cached client bound to the override model, so two stages on
    different models coexist without cache-replacing each other. The provider
    is inferred from the model name (so an opus override routes to Anthropic
    even when the global provider is OpenAI); key-fallback semantics match
    `get_llm_client()`.
    """
    if not model:
        return get_llm_client()

    from server.settings import settings

    fallback = settings.llm_fallback_model.strip()
    wkey = (model, fallback)
    wrapped = _wrapped_cache.get(wkey)
    if wrapped is not None:
        return wrapped
    return _remember(_wrapped_cache, wkey, _with_fallback(_override_client(model), model))


def get_llm_client_without_fallback(model: str, *, reasoning_effort: str = "") -> LLMClient:
    """The client for `model` alone, never wrapped in LOOM_LLM_FALLBACK_MODEL: for a
    stage whose thresholds were measured on this model, where another model's answer
    would be acted on as if it were this one's. `reasoning_effort` asks an OpenAI
    model for that effort (at least); other providers have no such field."""
    client = _override_client(model)
    if reasoning_effort and client.provider == "openai":
        from server.llm.openai import OpenAILLMClient

        return OpenAILLMClient(model=model, reasoning_effort=reasoning_effort)
    return client


# Wrapped (fallback-aware) override clients, keyed by (model, fallback_model).
_wrapped_cache: dict[tuple[str, str], LLMClient] = {}


def _override_client(model: str) -> LLMClient:
    """The plain client for a model override — built once per (provider,
    model), never wrapped. `get_llm_client_for_model` adds the fallback."""
    from server.settings import settings

    provider = provider_for_model(model, settings.llm_provider)
    key = (provider, model)
    cached = _override_cache.get(key)
    if cached is not None:
        return cached

    client = _build_llm_client(
        provider,
        model,
        fake_label=f"fake-echo-1[{model}]",
        warn=(
            f"LOOM_LLM_PROVIDER={provider} but its API key is not set; falling "
            f"back to the fake LLM client for stage override model={model!r}."
        ),
    )
    return _remember(_override_cache, key, client)
