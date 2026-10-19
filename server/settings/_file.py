"""The YAML settings file: `version: 1` and a flat `settings:` mapping of field
names (the `LOOM_<FIELD>` names, lowercase) to values.

It ranks below the dotenv file and the environment, above the defaults. Unlike
the environment, whose unknown `LOOM_*` names are only logged, the file is read
strictly: an unknown name, a secret or an unsupported version refuses to start,
since a file is written on purpose and a typo in it must not pass silently.
Secrets stay in the environment or a secrets manager, never in a file that is
committed. `schemas/loom-settings.schema.json` describes the file for editors.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from server.core.env_file import settings_file_path
from server.settings._env import is_secret_field

VERSIONS = (1,)


class SettingsFileError(ValueError):
    """The settings file cannot be used; the message names the file and the key."""


def _load_yaml(f: Any) -> Any:
    """`yaml.safe_load`, refusing a key given twice in one mapping, which the plain
    loader settles silently by keeping the last."""
    import yaml

    class _Strict(yaml.SafeLoader):
        pass

    def _mapping(loader: _Strict, node: yaml.MappingNode) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"{key!r} is given twice", key_node.start_mark
                )
            seen.add(key)
        return loader.construct_mapping(node)

    _Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    return yaml.load(f, Loader=_Strict)  # a SafeLoader subclass: no arbitrary objects


def read_settings_file(path: str, fields: set[str]) -> dict[str, Any]:
    """The `settings` mapping of the file at `path`, checked against `fields`."""
    import yaml

    try:
        with open(path, encoding="utf-8") as f:
            doc = _load_yaml(f)
    except (OSError, yaml.YAMLError) as e:
        raise SettingsFileError(f"{path}: not readable YAML ({e})") from e
    if not isinstance(doc, dict):
        raise SettingsFileError(f"{path}: expected a mapping with `version` and `settings`")
    extra = sorted(set(doc) - {"version", "settings"})
    if extra:
        raise SettingsFileError(f"{path}: unknown top-level keys {extra}")
    # `type(...) is int`: YAML's `true` is a bool, and True == 1 in Python.
    if type(doc.get("version")) is not int or doc.get("version") not in VERSIONS:
        raise SettingsFileError(
            f"{path}: version {doc.get('version')!r} is not supported; this build reads "
            f"version {', '.join(str(v) for v in VERSIONS)}"
        )
    values = doc.get("settings") or {}
    if not isinstance(values, dict):
        raise SettingsFileError(f"{path}: `settings` must be a mapping of setting names")
    unknown = sorted(str(k) for k in values if k not in fields)
    if unknown:
        raise SettingsFileError(f"{path}: no setting is named {', '.join(unknown)}")
    secrets = sorted(str(k) for k in values if is_secret_field(str(k)))
    if secrets:
        raise SettingsFileError(
            f"{path}: {', '.join(secrets)} is a secret: set it in the environment "
            "(LOOM_<NAME>) or a secrets manager, not in the settings file"
        )
    return dict(values)


class SettingsFileSource(PydanticBaseSettingsSource):
    """The settings file as a pydantic-settings source."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        path, named = settings_file_path()
        self._values: dict[str, Any] = {}
        if path is None:
            return
        if not os.path.isfile(path):
            if named:
                raise SettingsFileError(f"LOOM_SETTINGS_FILE names {path}, which does not exist")
            return
        self._values = read_settings_file(path, set(settings_cls.model_fields))

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)
