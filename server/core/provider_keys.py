"""Process-local provider credentials loaded from operator configuration.

The environment remains the fallback for deployments without saved settings.
Installing a snapshot never writes credentials back into os.environ.
"""

import os
from collections.abc import Mapping

KEY_NAMES: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "compatible": ("LOOM_EMBEDDING_API_KEY",),
}
# Hosted rerankers' keys: environment only, the dashboard does not save them.
RERANK_KEY_NAMES: dict[str, tuple[str, ...]] = {"cohere": ("COHERE_API_KEY",)}
_keys: dict[str, str] = {}


def provider_key(provider: str) -> str:
    if provider in _keys:
        return _keys[provider]
    return environment_provider_key(provider)


def environment_provider_key(provider: str) -> str:
    """Read deployment credentials without applying a saved dashboard override."""
    return next(
        (os.environ[name] for name in KEY_NAMES.get(provider, ()) if os.environ.get(name)), ""
    )


def rerank_key(provider: str) -> str:
    return next(
        (os.environ[name] for name in RERANK_KEY_NAMES.get(provider, ()) if os.environ.get(name)),
        "",
    )


def install_provider_keys(keys: Mapping[str, str]) -> None:
    """Replace the snapshot atomically; explicit empty keys do not fall back."""
    if set(keys) - KEY_NAMES.keys():
        raise ValueError("unknown provider credential")
    global _keys
    _keys = dict(keys)


def saved_key_presence() -> dict[str, bool]:
    """Credential-presence flags for diagnostics; never the secret values."""
    return {provider: bool(key) for provider, key in _keys.items()}
