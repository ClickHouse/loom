"""Whether the conflict judge runs: `LOOM_MEMORY_CONFLICT_JUDGE` when set, else the
LLM provider decides.

Unset, the judge is on while the resolved LLM provider is a real one (openai or
anthropic) whose key is present, from the environment or saved from the dashboard.
It is decided on each use, not at load: the dashboard can set the provider and its
key at runtime (`server/insights/app/provider_runtime.py`), and a stack booted on
the fake provider must pick the judge up once a key is saved. A real provider with
no key runs on the fake client, where every judge call would only fail open.
"""

from __future__ import annotations

REAL_LLM_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic"})


def conflict_judge_on() -> bool:
    """The switch every reader of the judge goes through."""
    from server.core.provider_keys import provider_key
    from server.settings import settings

    explicit = settings.memory_conflict_judge
    if explicit is not None:
        return bool(explicit)
    provider = settings.llm_provider
    return provider in REAL_LLM_PROVIDERS and bool(provider_key(provider))


def conflict_judge_reason() -> str:
    """Why the switch is where it is, for the boot log and diagnostics."""
    from server.core.provider_keys import KEY_NAMES, provider_key
    from server.settings import settings

    if settings.memory_conflict_judge is not None:
        return "LOOM_MEMORY_CONFLICT_JUDGE is set"
    provider = settings.llm_provider
    if provider not in REAL_LLM_PROVIDERS:
        return f"llm_provider={provider}"
    if not provider_key(provider):
        return f"llm_provider={provider} but {KEY_NAMES[provider][0]} is not set"
    return f"llm_provider={provider} with its key"
