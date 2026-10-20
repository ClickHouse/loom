"""Settings an operator may change while Loom runs.

A field annotated `Annotated[T, LIVE]` is read at call time, never captured at
import or boot, so a deployment-wide override (`server.core.settings_overrides`,
stored in loom.runtime_overrides) takes effect on the next read in every
process. Every other field is read from the environment once, at startup.
`tests/unit/test_live_settings.py` checks that each LIVE field is read only
inside a function body.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any

from pydantic import TypeAdapter


class _Live:
    def __repr__(self) -> str:
        return "LIVE"


LIVE = _Live()


def is_live(field: Any) -> bool:
    """`field` is a pydantic FieldInfo."""
    return any(m is LIVE for m in field.metadata)


@functools.cache
def _adapter(cls: type, name: str) -> TypeAdapter[Any]:
    field = cls.model_fields[name]  # type: ignore[attr-defined]
    annotation: Any = field.annotation
    if field.metadata:
        annotation = Annotated[annotation, *field.metadata]
    return TypeAdapter(annotation)


def coerce(cls: type, name: str, value: Any) -> Any:
    """`value` validated as field `name` of settings class `cls` validates it from
    the environment (constraints included); ValueError when it does not fit."""
    return _adapter(cls, name).validate_python(value)


def to_json(cls: type, name: str, value: Any) -> Any:
    """`value` of field `name` as JSON-compatible data."""
    return _adapter(cls, name).dump_python(value, mode="json")
