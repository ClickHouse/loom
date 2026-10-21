"""Environment-driven configuration for the Loom Control Plane.

One `Settings` object, composed from the sections of `server/settings/catalog.py`,
where every setting is declared. Every field keeps its flat name and its
`LOOM_<NAME>` env var: `settings.memory_ask_top_k` is one flat field whichever
section declares it. A value comes from, first match wins: the environment, the
dotenv file (`LOOM_ENV_FILE`), the YAML settings file (`LOOM_SETTINGS_FILE`,
`_file.py`), the default; a live override (`server.core.settings_overrides`) wins
over all of them while Loom runs.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from pydantic import PrivateAttr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import DotEnvSettingsSource

from server.core.env_file import env_file_path
from server.settings._env import DEV_JWT_SECRET, is_secret_field, unknown_env_keys
from server.settings._file import SettingsFileSource
from server.settings._loaded import LOADING, Recorded
from server.settings.catalog import (
    BUILTIN_COHERE,
    AuthSettings,
    ClickHouseSettings,
    CodeSettings,
    ContextSettings,
    EndUserIssuer,
    ErasureSettings,
    ExtractionSettings,
    KnowledgeSettings,
    McpSettings,
    MemoryAskSettings,
    MemorySearchSettings,
    MemorySettings,
    MemoryWebhookSettings,
    ModelSettings,
    NamespaceSettings,
    ObservabilitySettings,
    ProviderSettings,
    RateLimitSettings,
    RuntimeSettings,
    StorageSettings,
    TaskSettings,
    TrackerSettings,
    is_builtin_cohere,
)


def _named_in(dotenv_settings: PydanticBaseSettingsSource) -> str | None:
    """LOOM_SETTINGS_FILE as the dotenv file this load reads names it: pydantic's own
    merged read (keys lowercased), so `~` and several files resolve as for every value."""
    # pydantic-settings always passes its DotEnvSettingsSource here (2.5 onward).
    assert isinstance(dotenv_settings, DotEnvSettingsSource)
    return dotenv_settings.env_vars.get("loom_settings_file") or None


class Settings(
    ClickHouseSettings,
    ProviderSettings,
    ModelSettings,
    AuthSettings,
    RateLimitSettings,
    RuntimeSettings,
    ContextSettings,
    McpSettings,
    MemorySearchSettings,
    MemoryAskSettings,
    ExtractionSettings,
    CodeSettings,
    TrackerSettings,
    ObservabilitySettings,
    MemorySettings,
    KnowledgeSettings,
    TaskSettings,
    NamespaceSettings,
    ErasureSettings,
    StorageSettings,
    MemoryWebhookSettings,
    BaseSettings,
):
    # hide_input_in_errors: a load error names the field and the rule it broke,
    # never the values passed. A model-level check's error would otherwise print
    # the whole environment it was handed, secrets included, to the boot log.
    model_config = SettingsConfigDict(
        env_prefix="LOOM_",
        extra="ignore",
        validate_by_name=True,
        validate_by_alias=True,
        hide_input_in_errors=True,
    )
    # source -> the fields it supplied at load (`loaded_from`).
    _loaded_from: dict[str, frozenset[str]] = PrivateAttr(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # First wins: the environment, then the dotenv file, then the settings file.
        return (
            init_settings,
            Recorded(env_settings, "env"),
            Recorded(dotenv_settings, "env_file"),
            file_secret_settings,
            Recorded(SettingsFileSource(settings_cls, _named_in(dotenv_settings)), "settings_file"),
        )

    def __init__(self, **values: Any) -> None:
        LOADING.clear()
        super().__init__(**values)
        self._loaded_from = dict(LOADING)

    def loaded_from(self, source: str) -> frozenset[str]:
        """The fields `source` (env, env_file, env_copied_from_env_file, settings_file,
        env_copied_from_settings_file) supplied when this object loaded."""
        return self._loaded_from.get(source, frozenset())

    @model_validator(mode="after")
    def _resolve_conflict_resolver(self) -> Settings:
        """Unset, the conflict resolver is on unless the LLM provider or its model's
        provider is `fake`; an explicit value is never overridden. A missing key does
        not turn it off: writes are still marked, and the worker judges them once the
        key is there."""
        if "memory_conflict_resolver_enabled" not in self.model_fields_set:
            self.memory_conflict_resolver_enabled = "fake" not in (
                self.llm_provider,
                self.conflict_resolver_provider(),
            )
            self._inferred_fields |= {"memory_conflict_resolver_enabled"}
        return self

    def conflict_resolver_provider(self) -> str:
        """The provider the conflict resolver's model runs on."""
        from server.core.llm_provider import provider_for_model

        return provider_for_model(self.conflict_resolver_model, self.llm_provider)

    def inference_issuer(self) -> str:
        """The `iss` of the identity tokens Loom signs for an inference gateway
        (`inference_token_audience`): its public address under /v1, where the
        discovery document and the JWKS are served."""
        return f"{self.public_base_url.strip().rstrip('/')}/v1"

    @model_validator(mode="after")
    def _check_code_rerank_behind_the_gateway(self) -> Settings:
        """`builtin:cohere` calls Cohere's own API, and the code reranker fails
        open: while the gateway is required, the default may not name it."""
        why = self.gateway_required_by()
        if why and is_builtin_cohere(self.code_search_rerank_default.strip()):
            raise ValueError(
                f"{why} refuses LOOM_CODE_SEARCH_RERANK_DEFAULT=builtin:cohere: "
                "rerank through the gateway"
            )
        return self

    @model_validator(mode="after")
    def _check_inference_identity(self) -> Settings:
        """Signing its own identity tokens, Loom needs an https public address
        for their issuer (the gateway fetches the keys there, and a key swapped
        on the way would be trusted), and they must not pass for Loom's own
        tokens, which the same RS256 keys sign: the gateway would take those."""
        if not self.inference_token_audience:
            return self
        base = urlsplit(self.public_base_url.strip())
        if not base.hostname:
            raise ValueError(
                "LOOM_INFERENCE_TOKEN_AUDIENCE needs LOOM_PUBLIC_BASE_URL, the issuer of the "
                "identity tokens Loom signs (the gateway reads its keys there)"
            )
        if base.scheme != "https" and base.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("LOOM_INFERENCE_TOKEN_AUDIENCE needs an https LOOM_PUBLIC_BASE_URL")
        if self.inference_token_audience == self.jwt_audience:
            raise ValueError(
                "LOOM_INFERENCE_TOKEN_AUDIENCE is LOOM_JWT_AUDIENCE, the audience of Loom's "
                "own tokens; name the gateway's"
            )
        if self.inference_issuer() in self.trusted_issuers():
            raise ValueError(
                "the identity tokens' issuer (LOOM_PUBLIC_BASE_URL + /v1) is one Loom's own "
                "verifier trusts (LOOM_JWT_ISSUER, LOOM_JWT_TRUSTED_ISSUERS)"
            )
        return self

    def _mask(self, key: str, value: object) -> object:
        # Only the length, so an operator can confirm it is set: even a short
        # prefix leaks too much of a low-entropy secret.
        if value in ("", None) or not is_secret_field(key):
            return value
        return f"<masked, {len(str(value))} chars>"

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={self._mask(k, v)!r}" for k, v in self.model_dump().items())
        return f"Settings({fields})"

    __str__ = __repr__


settings = Settings(_env_file=env_file_path())


def log_unknown_env_keys() -> None:
    """One boot WARN naming the LOOM_* keys no setting reads."""
    unknown = unknown_env_keys(Settings.model_fields, env_file_path())
    if unknown:
        logging.getLogger(__name__).warning(
            "LOOM_* variables that match no setting are ignored: %s", ", ".join(unknown)
        )


__all__ = [
    "BUILTIN_COHERE",
    "DEV_JWT_SECRET",
    "EndUserIssuer",
    "Settings",
    "is_builtin_cohere",
    "log_unknown_env_keys",
    "settings",
]
