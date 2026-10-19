"""Which source supplied each field when `Settings` loaded: what `loom settings list`
reports as a value's source, without reading the environment or the files again
(they may have changed since, or no longer parse)."""

from __future__ import annotations

import os
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import PydanticBaseSettingsSource

from server.settings._live import coerce

# Filled by the sources while one `Settings()` loads, then moved onto that instance.
LOADING: dict[str, frozenset[str]] = {}


class Recorded(PydanticBaseSettingsSource):
    """`inner`, recording under `name` the fields it supplied. A file's value the
    environment also holds, unchanged, was copied there from the file (the CLI and
    compose copy the dotenv file, scripts/serve.sh the web tier's fields), so it
    stays the file's."""

    def __init__(self, inner: PydanticBaseSettingsSource, name: str) -> None:
        super().__init__(inner.settings_cls)
        self._inner, self._name = inner, name

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = self._inner()
        # A dotenv source also hands back the file's LOOM_* names no field reads.
        names = frozenset(values) & self.settings_cls.model_fields.keys()
        if self._name in ("env_file", "settings_file"):
            copied = frozenset(
                n
                for n in names
                if f"LOOM_{n.upper()}" in os.environ
                and _same(self.settings_cls, n, values[n], os.environ[f"LOOM_{n.upper()}"])
            )
            LOADING[f"env_copied_from_{self._name}"] = copied
        LOADING[self._name] = names
        return values


def _same(cls: type, name: str, file_value: Any, env_value: str) -> bool:
    """Whether the environment holds the file's value as the field reads it: a launcher
    exports the validated value (`web_workers: 3.0` as 3), a string compares without
    surrounding space (a launcher strips a path), and a case-folded mode reads `JSON`
    as `json`."""
    try:
        a, b = coerce(cls, name, file_value), coerce(cls, name, env_value)
    except ValueError:
        return False
    if isinstance(a, str) and isinstance(b, str):
        return a.strip() == b.strip()
    return bool(a == b)
