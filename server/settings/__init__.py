"""Environment-driven configuration for the Loom Control Plane.

One `Settings` object, composed from one mixin per area (`_clickhouse`,
`_memory_search`, `_code`, …). Every field keeps its flat name and its
`LOOM_<NAME>` env var: `settings.memory_ask_top_k` is one flat field
whichever mixin declares it — the split is for the reader, not the caller.
"""

from __future__ import annotations

import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

from server.core.env_file import env_file_path
from server.settings._auth import AuthSettings
from server.settings._clickhouse import ClickHouseSettings
from server.settings._code import CodeSettings
from server.settings._context import ContextSettings
from server.settings._env import DEV_JWT_SECRET, is_secret_field, unknown_env_keys
from server.settings._erasure import ErasureSettings
from server.settings._extraction import ExtractionSettings
from server.settings._knowledge import KnowledgeSettings
from server.settings._mcp import McpSettings
from server.settings._memory import MemorySettings
from server.settings._memory_ask import MemoryAskSettings
from server.settings._memory_search import MemorySearchSettings
from server.settings._models import ModelSettings
from server.settings._namespaces import NamespaceSettings
from server.settings._observability import ObservabilitySettings
from server.settings._providers import ProviderSettings
from server.settings._ratelimit import RateLimitSettings
from server.settings._runtime import RuntimeSettings
from server.settings._storage import StorageSettings
from server.settings._tasks import TaskSettings
from server.settings._tracker import TrackerSettings


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


# pydantic's mypy plugin reads a `Field(default, ge=…)` field as required;
# at runtime every field has its default.
settings = Settings(_env_file=env_file_path())  # type: ignore[call-arg]


def log_unknown_env_keys() -> None:
    """One boot WARN naming the LOOM_* keys no setting reads."""
    unknown = unknown_env_keys(Settings.model_fields, env_file_path())
    if unknown:
        logging.getLogger(__name__).warning(
            "LOOM_* variables that match no setting are ignored: %s", ", ".join(unknown)
        )


__all__ = ["DEV_JWT_SECRET", "Settings", "log_unknown_env_keys", "settings"]
