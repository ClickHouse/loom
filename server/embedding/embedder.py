"""Embedder protocol + selector.

`get_embedder()` returns the configured text embedder, cached per-process.
"""

from __future__ import annotations

import logging
from typing import Protocol

from server.core.embedding_space import inferred_provider
from server.core.provider_keys import provider_key
from server.embedding.alias import PREFIX as ALIAS_PREFIX
from server.llm import customer, gateway
from server.settings import settings


class Embedder(Protocol):
    """Contract for any embedding provider."""

    model_name: str
    dim: int

    async def embed(self, text: str) -> list[float]:
        """Return a unit-norm embedding vector for `text`; an empty or
        whitespace-only text may come back as an all-zeros vector (LocalEmbedder
        does), which has no norm to be unit."""
        ...

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in input order."""
        ...


_cached: Embedder | None = None


def provider_for(name: str) -> str:
    """Resolve the provider for the served model name `name`.

    `prefix:*` and `ctx*:*` labels resolve to "prefix" and "alias" whatever
    the setting says. Otherwise `LOOM_EMBEDDING_PROVIDER` wins if set, else
    the provider is inferred from the name (`_inferred_provider`).
    """
    label = _label_provider(name)
    if label:
        return label
    explicit = settings.embedding_provider.strip().lower()
    if explicit:
        if explicit not in {"fake", "local", "openai", "gemini", "compatible"}:
            raise ValueError(
                f"unknown LOOM_EMBEDDING_PROVIDER={explicit!r}; "
                "expected one of: fake, local, openai, gemini, compatible (or leave "
                "empty to infer from LOOM_EMBEDDING_MODEL)"
            )
        return explicit
    return _inferred_provider(name)


def _label_provider(name: str) -> str | None:
    if name.startswith("prefix:"):
        # A prefix twin of the served model; never a served
        # model itself, so the explicit provider does not apply to it
        return "prefix"
    if name.startswith(ALIAS_PREFIX) and ":" in name:
        # An alias label over the served model (rows whose
        # context text was rewritten; same vectors, same width)
        return "alias"
    return None


def _inferred_provider(name: str) -> str:
    """The provider a model name implies, ignoring `LOOM_EMBEDDING_PROVIDER`:
    fake, local, openai or gemini (or a label's prefix / alias)."""
    return _label_provider(name) or inferred_provider(name)


# The API-key environment variables each keyed provider reads, any one of
# which configures it. `/v1/health` and `loom doctor` read this table too.
KEY_ENVS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "compatible": ("LOOM_EMBEDDING_API_KEY",),
}


def embedder_is_fake() -> bool:
    """True when the configured embedding model is served by the fake n-gram embedder
    (no semantic vectors): a fake model or provider, or a keyed provider without its key.
    False for a model name no provider serves: building its embedder fails, so no fake
    serves it either."""
    try:
        provider = provider_for(settings.embedding_model)
    except ValueError:
        return False
    return provider == "fake" or _key_missing(provider)


def _key_missing(provider: str) -> bool:
    """A keyed provider with none of its keys set and no keyless gateway in front."""
    keyless = provider in ("openai", "compatible") and gateway.keyless()
    return bool(KEY_ENVS.get(provider)) and not provider_key(provider) and not keyless


def get_embedder() -> Embedder:
    """The configured text embedder, cached per process.

    An OpenAI or Gemini model without its API key falls back to the fake
    n-gram embedder at `LOOM_EMBEDDING_DIM` with an ERROR log, so the OSS dev
    path boots keyless.
    """
    global _cached
    if _cached is None:
        _cached = _served(settings.embedding_model)
    return _cached


def _served(name: str) -> Embedder:
    provider = provider_for(name)
    if provider in ("prefix", "alias"):
        # Labels over the served model, never the served model itself.
        raise ValueError(f"unknown embedding model: {name!r}")
    if _key_missing(provider):
        logging.getLogger(__name__).error(
            "No real embedder configured. LOOM_EMBEDDING_MODEL=%s but %s is not set — "
            "falling back to fake-ngram-%d (NOT production-quality; will mis-rank "
            "every search). Set the key, or use a keyless CPU embedder: "
            "LOOM_EMBEDDING_MODEL=local:bge-small (384-dim) or local:bge-base (768-dim), "
            "with the `local-embedder` extra (fastembed).",
            name,
            " / ".join(KEY_ENVS[provider]),
            settings.embedding_dim,
        )
        from server.embedding.fake import FakeNGramEmbedder

        # The configured width, like the explicit-fake branch: the
        # schema's vector columns are sized from it.
        return FakeNGramEmbedder(dim=settings.embedding_dim)
    if provider == "fake":
        from server.embedding.fake import FakeNGramEmbedder

        return FakeNGramEmbedder(dim=settings.embedding_dim)
    if provider == "local" and not (name == "local" or name.startswith("local:")):
        # LOOM_EMBEDDING_PROVIDER=local over a non-local model name.
        name = "local"
    return _build(name, provider)


def reset() -> None:
    """Test hook: drop cached embedders so settings changes take effect."""
    global _cached, _probed_dim
    _cached = None
    _probed_dim = None
    _secondary.clear()


# A code.search request may name a second vector model
# (`CodeSearchInput.vector_model`) whose rows sit under their own
# `embedding_model` label. Only specs in `settings.code_search_vector_models`
# are constructible; one instance per spec per process.
_secondary: dict[str, Embedder] = {}


def allowed_vector_models() -> list[str]:
    raw = settings.code_search_vector_models
    return [s.strip() for s in raw.split(",") if s.strip()]


def get_secondary_embedder(spec: str) -> Embedder:
    """The embedder for a request-named vector model. Raises ValueError when
    the spec is not in the deployment's allow-list, so a caller cannot make
    the server load an arbitrary model."""
    spec = (spec or "").strip()
    if spec not in allowed_vector_models():
        raise ValueError(
            f"vector_model {spec!r} is not enabled on this deployment "
            f"(LOOM_CODE_SEARCH_VECTOR_MODELS lists {allowed_vector_models() or 'nothing'})"
        )
    if spec == settings.embedding_model:
        # The served model: the same embedder, at the same width.
        return get_embedder()
    inst = _secondary.get(spec)
    if inst is None:
        # Inferred from the spec: LOOM_EMBEDDING_PROVIDER names the served
        # model's provider, and applied here it would turn a
        # `text-embedding-3-large` arm into a fake one or send `local:*` to
        # OpenAI.
        inst = _secondary[spec] = _build(spec, _inferred_provider(spec))
    return inst


def _build(spec: str, provider: str) -> Embedder:
    """An embedder for a spec from `provider`, no fallbacks: a missing key or
    model raises, because a secondary arm silently degraded to the fake
    embedder would measure nothing."""
    if provider == "compatible":
        from server.embedding.compatible import CompatibleEmbedder

        return CompatibleEmbedder(
            model=spec,
            dim=settings.embedding_dim,
            base_url=settings.embedding_base_url,
            api_key=provider_key("compatible"),
            send_dimensions=settings.embedding_send_dimensions,
        )
    if provider == "prefix":
        from server.embedding.prefix import PrefixEmbedder

        return PrefixEmbedder(base=get_embedder(), spec=spec)
    if provider == "alias":
        from server.embedding.alias import AliasEmbedder

        return AliasEmbedder(base=get_embedder(), spec=spec)
    if provider == "fake":
        from server.embedding.fake import FakeNGramEmbedder

        suffix = spec.rsplit("-", 1)[-1]
        return FakeNGramEmbedder(dim=int(suffix) if suffix.isdigit() else 1024)
    if provider == "local":
        from server.embedding.local import LocalEmbedder

        return LocalEmbedder(spec=spec)
    if provider == "openai":
        from server.embedding.openai import OpenAIEmbedder

        return OpenAIEmbedder(model=spec, dim=settings.embedding_dim)
    from server.embedding.gemini import GeminiEmbedder

    return GeminiEmbedder(model=spec, dim=settings.embedding_dim)


# Ground-truth vector length the configured embedder actually emits,
# populated by `probe_embedding_dim()` at boot. Authoritative over the
# operator-supplied `settings.embedding_dim`, which can be stale.
_probed_dim: int | None = None


async def probe_embedding_dim() -> int | None:
    """Embed a fixed string and return the vector length.

    Catches the mismatch where the model is changed but LOOM_EMBEDDING_DIM
    isn't — inserting wrong-length vectors hits Code 117 at write time.
    Returns None if the embedder is unavailable (callers fall back to
    `settings.embedding_dim`).
    """
    global _probed_dim
    try:
        embedder = get_embedder()
        with customer.system():  # the deployment's own check, no org's data
            vec = await embedder.embed("loom")
    except Exception as e:  # noqa: BLE001 — boot must survive an unreachable embedder
        logging.getLogger(__name__).warning(
            "embedding dim probe failed (%s); falling back to settings.embedding_dim=%d",
            e,
            settings.embedding_dim,
        )
        return None
    probed = len(vec) if vec else 0
    if probed <= 0:
        return None
    _probed_dim = probed
    if probed != settings.embedding_dim:
        logging.getLogger(__name__).warning(
            "embedding dim mismatch: LOOM_EMBEDDING_DIM=%d but the "
            "configured model %r emits %d-dim vectors. The probed "
            "dim is authoritative; set LOOM_EMBEDDING_DIM to match.",
            settings.embedding_dim,
            settings.embedding_model,
            probed,
        )
    return probed


def effective_embedding_dim() -> int:
    """Probed dim if available, else the configured scalar.

    Use this everywhere the actual emitted vector length matters
    (coded column widths, sanity checks on insert)."""
    return _probed_dim if _probed_dim is not None else settings.embedding_dim


def input_token_limit(embedder: Embedder) -> int | None:
    """The most tokens one input to `embedder` may hold, which its provider refuses past;
    None for a provider that states none Loom checks (local, compatible, fake). A prefix
    or alias label is its base model's."""
    while (base := getattr(embedder, "base", None)) is not None:
        embedder = base
    limit = getattr(embedder, "max_input_tokens", None)
    return int(limit) if limit else None
