"""Embedding and LLM providers."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, PrivateAttr, model_validator

from server.settings._env import CaseFolded, JsonObject, json_map_items
from server.settings._models import (
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_MODELS,
    ModelSettings,
)

# A configured rerank endpoint's name: what a request or a namespace sends instead of a URL.
RERANK_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class ProviderSettings(ModelSettings):
    # Inherits ModelSettings for `llm_model`, which the provider resolution below
    # reads and fills.
    # Vector width the embedder is asked for and the schema is sized from; the width probed from
    # the embedder at boot wins when they differ. Change it together with `embedding_model`.
    # Unset with a `local:*` model, it is that model's own width.
    embedding_dim: int = Field(3072, ge=1)

    # "" = infer from `embedding_model` (fake-ngram-* → fake, text-embedding-*
    # → openai, gemini* → gemini, local/local:* → local CPU embedder); or pin
    # "openai", "gemini", "local", "fake".
    # Switching providers changes the embedding space: a full re-embed is
    # required.
    embedding_provider: Annotated[
        Literal["", "openai", "gemini", "compatible", "local", "fake"], CaseFolded
    ] = ""
    # API root for an explicitly selected OpenAI-compatible embedding service,
    # e.g. https://provider.example/v1. Empty is invalid for compatible providers.
    embedding_base_url: str = ""
    # Send the optional dimensions request parameter to a compatible endpoint.
    # False uses its native output width; responses must still match embedding_dim.
    embedding_send_dimensions: bool = False
    # OpenAI's API root, for chat and embeddings: point it at an
    # OpenAI-compatible inference gateway in front of OpenAI (its /v1 root).
    openai_base_url: str = "https://api.openai.com/v1"
    # Anthropic's API root (the Messages API is <root>/v1/messages): point it at
    # a gateway in front of Anthropic. Off the default, the key also goes as a
    # bearer, the scheme gateways take.
    anthropic_base_url: str = "https://api.anthropic.com"
    # Model calls with no provider key: exchange the workload's OIDC token
    # (RFC 8693) at this token endpoint of the gateway, and send the answer as
    # the bearer (server/llm/gateway.py). The gateway must trust the workload's
    # issuer. "" = the provider keys.
    inference_token_url: str = ""
    # The workload's OIDC token, as a file its platform rotates (a projected
    # service-account token on Kubernetes). Needed with inference_token_url.
    inference_workload_token_file: str = ""
    # The scope sent with that exchange; "" sends none.
    inference_token_scope: str = ""
    # Shared query-embedding cache in loom.embedding_cache behind the
    # per-process LRU, keyed (model, sha1 of normalised text). Only single-text
    # (query) embeds up to `embedding_cache_shared_max_chars` (0 = no cap) use
    # it. Off = process cache only.
    embedding_cache_shared: bool = True
    # Longest query text, characters, that uses the shared cache; 0 = no cap.
    embedding_cache_shared_max_chars: int = 2000
    # The per-process query-embedding LRU: entries held (0 = cache nothing)
    # and seconds each lives.
    embedding_cache_size: int = Field(10_000, ge=0)
    # Seconds one per-process LRU entry lives.
    embedding_cache_ttl_s: float = 3600.0

    # Derive the similarity gates from this embedder's cosine distribution at
    # boot (server/memory/app/embedder_calibration.py). False pins the scalar
    # defaults (tuned for text-embedding-3-small) — re-bench after an embedder
    # change. Fake embedders and probe failures always use the scalars.
    adaptive_thresholds: bool = True
    # Seconds between each replica's re-read of the shared calibration
    # (loom.embedder_calibration), so every replica serves the same gates.
    calibration_refresh_seconds: float = 60.0

    # "openai" | "anthropic" | "fake"; "" = infer from LOOM_LLM_MODEL's name
    # (gpt*/o1*/o3*/o4*/chatgpt* → openai, claude* → anthropic, fake* → fake), else from the key
    # present (OPENAI_API_KEY, then ANTHROPIC_API_KEY), else fake. Resolved once
    # at load; an explicit value is never overridden. Run `make bench-quick`
    # before making anthropic a default.
    llm_provider: str = ""
    # A fake or key-less provider is a `warn` in /v1/health `problems`; true
    # (production) makes it an `error` and `status: degraded`, which preflight
    # and monitors catch. Never changes the HTTP code (503 is ClickHouse-only).
    require_real_providers: bool = False
    # What decided `llm_provider` — "LOOM_LLM_PROVIDER" when
    # explicit, else the evidence ("LOOM_LLM_MODEL=…", "OPENAI_API_KEY is set",
    # "nothing set"). Read through `llm_provider_source()`; not an env var.
    _llm_provider_source: str = PrivateAttr(default="LOOM_LLM_PROVIDER")
    # Fields the env left unset and load filled in (the LLM pair, a local
    # model's width): assigning them marks them set, but they are not the env's.
    _inferred_fields: frozenset[str] = PrivateAttr(default=frozenset())

    @model_validator(mode="after")
    def _resolve_local_embedding_dim(self) -> ProviderSettings:
        """A local model emits its own width, whatever LOOM_EMBEDDING_DIM says, and
        the schema is sized from `embedding_dim`: unset, it takes the model's."""
        if "embedding_dim" in self.model_fields_set:
            return self
        name = self.embedding_model
        if name == "local" or name.startswith("local:"):
            alias = name.partition(":")[2].strip() or DEFAULT_LOCAL_EMBEDDING_MODEL
        elif self.embedding_provider == "local":
            alias = DEFAULT_LOCAL_EMBEDDING_MODEL
        else:
            return self
        if alias in LOCAL_EMBEDDING_MODELS:  # an unknown alias fails when the embedder is built
            self.embedding_dim = LOCAL_EMBEDDING_MODELS[alias][1]
            self._inferred_fields |= {"embedding_dim"}
        return self

    @model_validator(mode="after")
    def _check_gateway(self) -> ProviderSettings:
        """A mistyped API root or a token exchange missing its token fails at
        load, not at the first model call."""
        urls = {
            "openai_base_url": self.openai_base_url,
            "anthropic_base_url": self.anthropic_base_url,
            "inference_token_url": self.inference_token_url,
        }
        for name, value in urls.items():
            if value and urlsplit(value).scheme not in ("https", "http"):
                raise ValueError(f"LOOM_{name.upper()} must be an http(s) URL, got {value!r}")
        if self.inference_token_url and not self.inference_workload_token_file:
            raise ValueError(
                "LOOM_INFERENCE_TOKEN_URL needs LOOM_INFERENCE_WORKLOAD_TOKEN_FILE, "
                "the token it exchanges"
            )
        return self

    @model_validator(mode="after")
    def _resolve_llm_provider(self) -> ProviderSettings:
        """Resolve an unset LLM provider / model once, at load,
        so `settings.llm_provider` is concrete everywhere (client factory,
        /v1/health, `loom doctor`) and the three never disagree."""
        from server.core.llm_provider import DEFAULT_MODEL_BY_PROVIDER, resolve_llm_provider

        # An explicit provider is never overridden, so a typo (`openia`) would
        # load as itself with no model and only fail at the first LLM call.
        explicit = (self.llm_provider or "").strip().lower()
        if explicit and explicit not in DEFAULT_MODEL_BY_PROVIDER:
            raise ValueError(
                f"unknown LOOM_LLM_PROVIDER={explicit!r}; expected one of: "
                f"{', '.join(DEFAULT_MODEL_BY_PROVIDER)} (or leave empty to infer "
                "from LOOM_LLM_MODEL or the API key present)"
            )
        # The pair moves together: an env that names either set both.
        pair = frozenset({"llm_provider", "llm_model"})
        inferred = pair if not (pair & self.model_fields_set) else frozenset()
        resolved = resolve_llm_provider(self.llm_provider, self.llm_model)
        self.llm_provider = resolved.provider
        self.llm_model = resolved.model
        self._llm_provider_source = resolved.source
        self._inferred_fields |= inferred
        return self

    def env_fields_set(self) -> frozenset[str]:
        """The fields this process's env (or constructor) set, without those
        load inferred."""
        return frozenset(self.model_fields_set) - self._inferred_fields

    def llm_provider_inferred(self) -> bool:
        """True when LOOM_LLM_PROVIDER was unset and the provider came from
        the model name or a key — the case `loom doctor`
        and the health problem list call out when the key is also missing."""
        return self._llm_provider_source != "LOOM_LLM_PROVIDER"

    def llm_provider_source(self) -> str:
        """The evidence behind `llm_provider`, for logs."""
        return self._llm_provider_source

    # Reasoning effort sent to OpenAI models. "" = the lowest the model accepts
    # (learned from its refusals, see server/llm/traits.py); set to pin one value
    # for every call.
    llm_openai_reasoning_effort: str = ""
    # Thinking effort sent to Claude models as `output_config.effort`: "low" |
    # "medium" | "high"; "" = send none (the model's own default). A model that
    # does not take it refuses it once and is not sent it again.
    llm_anthropic_effort: str = ""
    # Most output tokens a call adds over the caller's cap for a model's hidden
    # reasoning. The reserve per model is learned from the reasoning tokens its
    # replies report (twice the most it has spent) and capped here.
    llm_hidden_reasoning_reserve_max: int = Field(16384, ge=0)
    # Seconds an LLM call waits for its reply before the client retries. Generous
    # on purpose: it only catches a hung connection, so a slow model or a long
    # output never hits it. Calls that must be fast set their own, shorter
    # deadline (HyDE, search budgets).
    llm_request_timeout_s: float = Field(300.0, gt=0)

    # JSON {class: {"llm_cache": bool, "strong": bool, "mid": bool}}:
    # `llm_cache` overrides the class's cache default (a per-request pin
    # wins); `strong`/`mid` false bars the cascade from that tier. Unlisted =
    # every tier eligible.
    llm_policy_by_traffic_class: Annotated[str, JsonObject] = '{"bench": {"llm_cache": true}}'
    # JSON {name: url}: the rerank endpoints a request's `rerank`, a namespace's
    # `rerank_endpoint` and LOOM_CODE_SEARCH_RERANK_DEFAULT may name. Names are
    # short identifiers, URLs http(s). Loom posts candidate text only to these.
    rerank_endpoints_json: Annotated[str, JsonObject] = "{}"

    def rerank_endpoint_map(self) -> dict[str, str]:
        """The operator's rerank endpoints, name → URL."""
        raw = self.rerank_endpoints_json.strip()
        if not raw:
            return {}
        return dict(json_map_items(raw, "rerank_endpoints_json", non_empty=False))

    @model_validator(mode="after")
    def _check_rerank_endpoints(self) -> ProviderSettings:
        """Every name is a short identifier and every URL http(s) with a host. The
        error names the entry, never the URL (it can carry a credential)."""
        for name, url in self.rerank_endpoint_map().items():
            if not RERANK_NAME_RE.fullmatch(name) or name == "none":
                raise ValueError(
                    f"LOOM_RERANK_ENDPOINTS_JSON: {name[:64]!r} is not a valid name "
                    "(letters, digits, '_' or '-', at most 64, not 'none')"
                )
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"LOOM_RERANK_ENDPOINTS_JSON: {name!r} is not an http(s) URL")
        return self

    # Skip the rerank when the vector top hit's cosine is ≥ this (strong
    # matches rank reliably; the rerank costs ~500 ms). Fallback when the
    # adaptive gate has no probe.
    llm_rerank_skip_above: float = 0.62

    # Recovery worker: retries failed embeds and reconciles orphan
    # scope='shared' rows (the write path embeds inline).
    embedding_worker_enabled: bool = True
    # Seconds between recovery worker passes while there is work.
    embedding_worker_interval_seconds: int = 5
    # Sleep after an idle pass (nothing to embed); the 5 s interval resumes
    # once a pass writes a row.
    embedding_worker_idle_interval_seconds: int = 30
    # Orphan reconciliation: the full memory × embedding anti-join runs once
    # per `full_scan_seconds`; other passes check only memories of the last
    # `recent_seconds`. 0 = full join every pass.
    embedding_worker_orphan_full_scan_seconds: int = 1800
    # Age window, seconds, of the memories a non-full orphan pass checks.
    embedding_worker_orphan_recent_seconds: int = 900
    # The table the wrapper reads and writes; an isolated server whose boot
    # migrations are off points this at its own scratch mirror.
    llm_response_cache_table: str = "loom.llm_response_cache"
