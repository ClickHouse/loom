"""Validated input for the operator's model setup flow.

These models describe onboarding, not the full set of development providers:
the local embedder is a setup choice (the released stack's default), the fake one
is not.
"""

from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, model_validator

from server.settings.catalog import DEFAULT_LOCAL_EMBEDDING_MODEL, LOCAL_EMBEDDING_MODELS

ModelName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
ProviderName = Literal["openai", "anthropic", "gemini", "compatible"]
ModelJob = Literal[
    "llm_fallback_model",
    "memory_ask_synth_model",
    "memory_ask_synth_model_cheap",
    "memory_ask_mid_model",
    "profile_consolidation_model",
    "reflection_distill_model",
    "code_link_revise_model",
    "session_brief_llm_model",
    "code_ask_llm_model",
    "code_search_hyde_model",
    "code_search_concept_expand_model",
    "code_bisect_llm_rerank_model",
    "code_summary_model",
    "tracker_dupcheck_rerank_model",
    "tracker_dupcheck_judge_model",
    "retrieval_judge_model",
]


class SetupModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LLMSetup(SetupModel):
    provider: Literal["openai", "anthropic"]
    model: ModelName
    # Omitted jobs inherit the main model or retain their disabled state.
    overrides: dict[ModelJob, ModelName] = Field(default_factory=dict)
    verification_models: list[ModelName] | None = Field(default=None, max_length=8)


def local_width(model: str) -> int | None:
    """The width of a `local` / `local:<alias>` model, None for any other name."""
    if model == "local":
        alias = DEFAULT_LOCAL_EMBEDDING_MODEL
    elif model.startswith("local:"):
        alias = model.removeprefix("local:")
    else:
        return None
    known = LOCAL_EMBEDDING_MODELS.get(alias)
    return known[1] if known else None


class EmbeddingSetup(SetupModel):
    provider: Literal["openai", "gemini", "compatible", "local"]
    model: ModelName
    dimensions: int = Field(default=3072, ge=1, le=65535, strict=True)
    base_url: str = Field(default="", max_length=2048)
    send_dimensions: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self) -> "EmbeddingSetup":
        self.base_url = self.base_url.strip().rstrip("/")
        if self.provider == "local":
            width = local_width(self.model)
            if width is None:
                aliases = ", ".join(f"local:{alias}" for alias in sorted(LOCAL_EMBEDDING_MODELS))
                raise ValueError(f"A local model is `local` or one of {aliases}")
            if self.dimensions != width or self.base_url or self.send_dimensions:
                raise ValueError(
                    f"{self.model} embeds {width} dimensions and takes no endpoint options"
                )
            return self
        if self.provider != "compatible":
            if self.dimensions > 3072:
                raise ValueError("Built-in embedding providers support at most 3072 dimensions")
            if self.base_url or self.send_dimensions:
                raise ValueError("Custom endpoint options require the compatible provider")
            return self
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Specify an HTTP(S) API root without URL credentials, query or fragment"
            )
        _ = parsed.port
        return self


class ProviderSetupInput(SetupModel):
    llm: LLMSetup
    embedding: EmbeddingSetup
    # Omission means retain an existing credential. Never return this field.
    # SecretStr masks repr/JSON; handlers must also sanitize validation errors.
    keys: dict[ProviderName, SecretStr] = Field(default_factory=dict, repr=False, exclude=True)
    # Decimal string: UInt64 row versions exceed JavaScript's safe integer range.
    expected_version: str = Field(default="0", pattern=r"^[0-9]{1,20}$")


class ProviderChangeStatus(SetupModel):
    config_id: str
    state: str
    error_code: str
    progress: dict[str, str | int] = Field(default_factory=dict)


class ProviderSetupStatus(SetupModel):
    version: str
    source: Literal["environment", "dashboard"]
    llm_provider: str
    llm_model: str
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    embedding_base_url: str = ""
    embedding_send_dimensions: bool = False
    configured_keys: dict[str, bool]
    requires_setup: bool
    llm_overrides: dict[str, str] = Field(default_factory=dict)
    verification_models: list[str] | None = None
    pending: ProviderChangeStatus | None = None
    # Non-empty when this storage refuses an embedding change; the reason to show.
    embedding_change_blocked: str = ""


class ProviderSaveResult(SetupModel):
    config_id: str
    state: Literal["active", "queued"]


class ProviderResetInput(SetupModel):
    expected_version: str = Field(pattern=r"^[0-9]{1,20}$")


class ProviderRetryInput(SetupModel):
    config_id: UUID


class ProviderCheck(SetupModel):
    role: Literal["llm", "embedding"]
    provider: str
    model: str
    ready: bool
    code: Literal[
        "ready",
        "missing_key",
        "unsupported_provider",
        "invalid_credentials",
        "quota_or_rate_limit",
        "model_unavailable",
        "timeout",
        "invalid_output",
        "unsupported_dimensions",
        "gateway_required",
        "provider_error",
    ]
    # Status code or expected/actual width; never upstream text, which may echo secrets.
    detail: str = ""


class ProviderValidation(SetupModel):
    ready: bool
    checks: list[ProviderCheck]
