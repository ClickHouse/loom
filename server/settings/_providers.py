"""Embedding and LLM providers."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, PrivateAttr, model_validator

from server.core.embedding_space import inferred_provider
from server.settings._env import CaseFolded, JsonObject, json_map_items
from server.settings._models import (
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_MODELS,
    ModelSettings,
)


def url_host(url: str) -> str:
    """The host httpx sends `url` to, as LOOM_INFERENCE_GATEWAY_HOSTS names it:
    ASCII (an IDN as punycode), lower-case, no trailing dot; "" when httpx
    cannot parse it. httpx's own parser, so the check and the send never read
    a URL differently (several `@`, a backslash)."""
    try:
        return httpx.URL(url.strip()).raw_host.decode("ascii").lower().rstrip(".")
    except (httpx.InvalidURL, UnicodeDecodeError):
        return ""


# A DNS name LOOM_INFERENCE_GATEWAY_HOSTS may name: no scheme, port, path or wildcard.
_GATEWAY_HOST_RE = re.compile(r"[a-z0-9][a-z0-9.-]*")


def _is_gateway_host(host: str) -> bool:
    """A bare DNS name, or an IPv6 address (its colons are not a port)."""
    if ":" not in host:
        return bool(_GATEWAY_HOST_RE.fullmatch(host))
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


# The settings the gateway check reads; a dashboard configuration overrides the embedding ones.
_GATEWAY_CHECKED = (
    "openai_base_url",
    "anthropic_base_url",
    "embedding_base_url",
    "inference_token_url",
    "embedding_provider",
    "embedding_model",
)


# The rerank choice that calls Cohere's own API, optionally `:<model>`.
BUILTIN_COHERE = "builtin:cohere"


def is_builtin_cohere(name: str) -> bool:
    """Whether a rerank choice is `builtin:cohere[:<model>]`, which calls Cohere's own API."""
    return name == BUILTIN_COHERE or name.startswith(BUILTIN_COHERE + ":")


# A configured rerank endpoint's name: what a request or a namespace sends instead of a URL.
RERANK_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


# RFC 9110 token characters: what an HTTP header name may hold.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# What a customer or feature header value carries unencoded (`customer.headers`).
_HEADER_TEXT = re.compile(r"[A-Za-z0-9._~:@-]*")
# A customer or feature name sent as a header value: the same characters, at least one.
_HEADER_VALUE = re.compile(r"[A-Za-z0-9._~:@-]+")
# Headers the model-call senders set themselves, or the HTTP client does: a customer or
# feature header of that name would overwrite them (the provider key, the body's type).
_SENDER_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-goog-api-key",
        "anthropic-version",
        "content-type",
        "content-length",
        "host",
    }
)


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
    # Model calls with no provider key: exchange an identity token (RFC 8693)
    # at this token endpoint of the gateway, and send the answer as the bearer
    # (server/llm/gateway.py). The gateway must trust the token's issuer. "" =
    # the provider keys.
    inference_token_url: str = ""
    # The workload's OIDC token, as a file its platform rotates (a projected
    # service-account token on Kubernetes). With inference_token_url, this or
    # inference_token_audience.
    inference_workload_token_file: str = ""
    # With inference_token_url and no workload token: the audience of the identity token Loom
    # signs (issuer <https public_base_url>/v1, newest RS256 key); never jwt_audience.
    inference_token_audience: str = ""
    # The scope sent with that exchange; "" sends none.
    inference_token_scope: str = ""
    # True: model calls and memory content go only to inference_gateway_hosts;
    # on whenever inference_token_url is set.
    inference_gateway_required: bool = False
    # Comma-separated hostnames (exact, any case) of the gateway; required, and
    # the only hosts sent to, while the gateway is required.
    inference_gateway_hosts: str = ""
    # Header naming the customer (the org) on every model call, for a gateway that
    # meters by it; set, a call naming no customer is refused (server/llm/customer.py).
    inference_customer_header: str = ""
    # Header naming the feature that made the call (hyde, memory.search, worker.embedding, ...),
    # with inference_customer_header only; "" sends none.
    inference_feature_header: str = ""
    # The customer named on calls made for the deployment itself (startup probe, the
    # recalibration worker's fixed pairs, provider checks), which belong to no org.
    inference_system_customer: str = "loom"
    # True (a cell serving several customers): refuse to start without
    # inference_customer_header, so no model call goes out unattributed.
    inference_customer_required: bool = False
    # How the customer header names an org, as the gateway knows it: `{org}` is the
    # org id, around it letters, digits and . _ ~ : @ - only ("{org}" = the id as is).
    inference_customer_format: str = "{org}"
    # JSON object from Loom's feature names (hyde, memory.search, worker.embedding, ...)
    # to the gateway's (letters, digits, . _ ~ : @ -); "*" for any other; "" = Loom's.
    inference_feature_map_json: Annotated[str, JsonObject] = ""
    # Shared query-embedding cache in loom.embedding_cache behind the per-process LRU,
    # rows keyed (model, sha1(org, model, version salt, normalised text)); a call outside
    # a request skips both tiers. Only single-text (query) embeds up to
    # `embedding_cache_shared_max_chars` (0 = no cap) use it. Off = process cache only.
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

    def feature_map(self) -> dict[str, str]:
        """`inference_feature_map_json` parsed: Loom feature -> the gateway's."""
        raw = self.inference_feature_map_json.strip()
        if not raw:
            return {}
        return dict(json_map_items(raw, "LOOM_INFERENCE_FEATURE_MAP_JSON", strings_only=True))

    @model_validator(mode="after")
    def _check_customer_headers(self) -> ProviderSettings:
        """Refuses a configuration under which a call could not name its customer
        as the gateway reads it."""
        for field in ("inference_customer_header", "inference_feature_header"):
            name = getattr(self, field)
            if name and not _HEADER_NAME.fullmatch(name):
                raise ValueError(f"LOOM_{field.upper()} must be an HTTP header name, got {name!r}")
            if name.lower() in _SENDER_HEADERS:
                raise ValueError(
                    f"LOOM_{field.upper()} must not be {name!r}, a header the model-call "
                    "senders set themselves"
                )
        customer, feature = self.inference_customer_header, self.inference_feature_header
        if feature and not customer:
            raise ValueError(
                "LOOM_INFERENCE_FEATURE_HEADER needs LOOM_INFERENCE_CUSTOMER_HEADER: "
                "without it no metering header is sent"
            )
        if customer and feature and customer.lower() == feature.lower():
            raise ValueError(
                "LOOM_INFERENCE_FEATURE_HEADER must differ from LOOM_INFERENCE_CUSTOMER_HEADER, "
                "or the feature overwrites the customer"
            )
        if self.inference_customer_required and not customer:
            raise ValueError(
                "LOOM_INFERENCE_CUSTOMER_REQUIRED needs LOOM_INFERENCE_CUSTOMER_HEADER: "
                "the header the gateway meters customers by"
            )
        fmt = self.inference_customer_format
        if "{org}" not in fmt or not _HEADER_TEXT.fullmatch(fmt.replace("{org}", "")):
            raise ValueError(
                "LOOM_INFERENCE_CUSTOMER_FORMAT must contain {org}, and around it only "
                "letters, digits and . _ ~ : @ -, which a header carries as they are"
            )
        if not all(_HEADER_VALUE.fullmatch(v) for v in self.feature_map().values()):
            raise ValueError(
                "LOOM_INFERENCE_FEATURE_MAP_JSON: each gateway feature name is letters, digits "
                "and . _ ~ : @ -, which a header carries as they are"
            )
        if self.inference_customer_header and not _HEADER_VALUE.fullmatch(
            self.inference_system_customer
        ):
            raise ValueError(
                "LOOM_INFERENCE_SYSTEM_CUSTOMER must name a customer while "
                "LOOM_INFERENCE_CUSTOMER_HEADER is set: letters, digits and . _ ~ : @ -"
            )
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
            parts = urlsplit(value)
            if value and (parts.scheme not in ("https", "http") or not parts.hostname):
                raise ValueError(f"LOOM_{name.upper()} must be an http(s) URL, got {value!r}")
        token = urlsplit(self.inference_token_url)
        if token.scheme == "http" and token.hostname not in ("localhost", "127.0.0.1", "::1"):
            # The identity token goes there.
            raise ValueError("LOOM_INFERENCE_TOKEN_URL must be https")
        if self.inference_token_audience and not self.inference_token_url:
            raise ValueError("LOOM_INFERENCE_TOKEN_AUDIENCE needs LOOM_INFERENCE_TOKEN_URL")
        sources = bool(self.inference_workload_token_file) + bool(self.inference_token_audience)
        if self.inference_token_url and sources != 1:
            raise ValueError(
                "LOOM_INFERENCE_TOKEN_URL needs one of LOOM_INFERENCE_WORKLOAD_TOKEN_FILE (the "
                "workload's token) and LOOM_INFERENCE_TOKEN_AUDIENCE (a token Loom signs)"
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
        return dict(json_map_items(raw, "rerank_endpoints_json"))

    def gateway_required(self) -> bool:
        """Whether model calls may go only to the gateway's hosts."""
        return bool(self.gateway_required_by())

    def gateway_hosts(self) -> frozenset[str]:
        """The hosts of inference_gateway_hosts, as `url_host` spells them."""
        return frozenset(
            h.strip().lower().rstrip(".")
            for h in self.inference_gateway_hosts.split(",")
            if h.strip()
        )

    def off_gateway(self, url: str) -> str:
        """While the gateway is required, the host of a `url` it does not list
        ("(none)" for a URL with no host); "" when the send may go."""
        if not self.gateway_required():
            return ""
        host = url_host(url)
        return "" if host in self.gateway_hosts() else host or "(none)"

    def gateway_refusal(self, changes: Mapping[str, Any] | None = None) -> str:
        """Why these settings, with `changes` (a dashboard configuration) over
        them, would send a model call off the gateway; "" when none would."""
        why = self.gateway_required_by()
        if not why:
            return ""
        if not self.gateway_hosts():
            return f"{why} needs LOOM_INFERENCE_GATEWAY_HOSTS, the hosts model calls may go to"
        saved = dict(changes or {})
        v = self.model_dump(include=set(_GATEWAY_CHECKED)) | saved
        # Both API roots count, a provider unused included: a model named per
        # request may be either provider's.
        for field, what in (
            ("openai_base_url", "LOOM_OPENAI_BASE_URL"),
            ("anthropic_base_url", "LOOM_ANTHROPIC_BASE_URL"),
            ("embedding_base_url", "LOOM_EMBEDDING_BASE_URL"),
        ):
            host = self.off_gateway(v[field]) if v[field] else ""
            if host:
                if field in saved:
                    what = "the saved embedding base URL"
                return f"{why} needs {what} at a host in LOOM_INFERENCE_GATEWAY_HOSTS, not {host}"
        host = self.off_gateway(v["inference_token_url"]) if v["inference_token_url"] else ""
        if host:
            return (
                f"LOOM_INFERENCE_GATEWAY_HOSTS does not list {host}, the host of "
                "LOOM_INFERENCE_TOKEN_URL: the token exchange goes to the gateway too"
            )
        for name, url in self.rerank_endpoint_map().items():
            if self.off_gateway(url):
                # The entry, never its URL: it can carry a credential.
                return (
                    f"{why} refuses LOOM_RERANK_ENDPOINTS_JSON entry {name!r}: not a gateway host"
                )
        provider: str = v["embedding_provider"]
        if not provider:
            try:
                provider = inferred_provider(v["embedding_model"])
            except ValueError:
                provider = ""  # an unknown model is refused where the embedder is built
        if provider == "gemini":
            # The Gemini embedder calls Google's own API, never a gateway.
            which = (
                "the saved Gemini embedder"
                if "embedding_provider" in saved
                else "the Gemini embedder"
            )
            return f"{why} refuses {which}: embed through the gateway"
        return ""

    def gateway_required_by(self) -> str:
        """The setting that requires the gateway, for an error to name; "" when none does."""
        if self.inference_token_url:
            return "LOOM_INFERENCE_TOKEN_URL"
        return "LOOM_INFERENCE_GATEWAY_REQUIRED" if self.inference_gateway_required else ""

    @model_validator(mode="after")
    def _check_gateway_hosts(self) -> ProviderSettings:
        """Every gateway host is a bare hostname, and with the gateway required
        every address Loom sends model calls to is one of them (the senders
        refuse any other too, on every redirect hop)."""
        for host in self.gateway_hosts():
            if not _is_gateway_host(host):
                raise ValueError(
                    f"LOOM_INFERENCE_GATEWAY_HOSTS: {host[:64]!r} is not a hostname "
                    "(no scheme, port, path or wildcard)"
                )
        refusal = self.gateway_refusal()
        if refusal:
            raise ValueError(refusal)
        return self

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
