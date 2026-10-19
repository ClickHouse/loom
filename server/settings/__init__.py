"""Environment-driven configuration for the Loom Control Plane.

One `Settings` object, composed from one mixin per area (`_clickhouse`,
`_memory_search`, `_code`, …). Every field keeps its flat name and its
`LOOM_<NAME>` env var: `settings.memory_ask_top_k` is one flat field
whichever mixin declares it — the split is for the reader, not the caller.
A value comes from, first match wins: the environment, the dotenv file
(`LOOM_ENV_FILE`), the YAML settings file (`LOOM_SETTINGS_FILE`, `_file.py`),
the default.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from server.core.env_file import env_file_path
from server.settings._auth import AuthSettings
from server.settings._clickhouse import ClickHouseSettings
from server.settings._code import CodeSettings
from server.settings._context import ContextSettings
from server.settings._env import DEV_JWT_SECRET, is_secret_field, unknown_env_keys
from server.settings._erasure import ErasureSettings
from server.settings._extraction import ExtractionSettings
from server.settings._file import SettingsFileSource
from server.settings._knowledge import KnowledgeSettings
from server.settings._loaded import LOADING, Recorded
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
            Recorded(SettingsFileSource(settings_cls), "settings_file"),
        )

    def __init__(self, **values: Any) -> None:
        LOADING.clear()
        super().__init__(**values)
        self._loaded_from = dict(LOADING)

    def loaded_from(self, source: str) -> frozenset[str]:
        """The fields `source` (env, env_file, env_copied_from_file, settings_file)
        supplied when this object loaded."""
        return self._loaded_from.get(source, frozenset())

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
    """One boot WARN naming the LOOM_* keys no setting reads, and one when the
    dotenv file names the settings file, which only the environment can."""
    log = logging.getLogger(__name__)
    unknown = unknown_env_keys(Settings.model_fields, env_file_path())
    if unknown:
        log.warning("LOOM_* variables that match no setting are ignored: %s", ", ".join(unknown))
    path = env_file_path()
    if path and os.path.isfile(path) and "LOOM_SETTINGS_FILE" not in os.environ:
        from dotenv import dotenv_values

        if "LOOM_SETTINGS_FILE" in dotenv_values(path):
            log.warning(
                "LOOM_SETTINGS_FILE in %s is ignored: set it in the environment "
                "(the settings file is chosen before the dotenv file is read)",
                path,
            )


__all__ = ["DEV_JWT_SECRET", "Settings", "log_unknown_env_keys", "settings"]
