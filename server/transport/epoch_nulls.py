"""A ClickHouse zero date is "never", not a date.

A `DateTime` column defaults to 0, which reads back as 1970-01-01 00:00:00.
Rows that never had the event (a memory never recalled, a PR whose open time
was not recorded) would answer `"last_recalled_at": "1970-01-01T00:00:00"` or
`"opened_at": "1970-01-01 00:00:00"` — a date an agent reads as real.

At the response boundary both transports map that value to null, driven by
the op's output model:

- a typed field whose annotation admits None (`datetime | None`, `str |
  None`, `Any`) carries null for an epoch value;
- a field that does not admit None is left alone — a required timestamp at
  1970 is a bug to see, not to hide;
- inside untyped content (`dict[str, Any]`, `list[dict]`, `Any`) only keys
  that name a time (`*_at`, `ts`) are mapped, so a stored memory VALUE that
  happens to read "1970-01-01" is never touched.
"""

from __future__ import annotations

import datetime as _dt
import re
import types
import typing
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

# Fields that carry a caller's own payload verbatim (a memory value, a task
# brief or outcome, observation input/output). What a caller stored is
# returned as stored, even a key named `*_at` holding the zero date.
PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "value",
        "content",
        "brief",
        "outcome",
        "metadata",
        "tool_input",
        "tool_output",
        "data",
        "body",
        "payload",
        "args",
        "attributes",
        "properties",
        "record",
        "fields",
    }
)

_EPOCH_RE = re.compile(r"^1970-01-01(?:[ T]00:00:00(?:\.0+)?(?:Z|[+-]00:?00)?)?$")


def is_epoch(value: Any) -> bool:
    """True for the zero date in any of the shapes it reaches a response in."""
    if isinstance(value, _dt.datetime):
        return value.replace(tzinfo=None) == _dt.datetime(1970, 1, 1)
    if isinstance(value, _dt.date):
        return value == _dt.date(1970, 1, 1)
    return isinstance(value, str) and bool(_EPOCH_RE.match(value))


def _time_key(key: Any) -> bool:
    return isinstance(key, str) and (key.endswith("_at") or key == "ts")


def _loose(data: Any, key: Any = None) -> Any:
    """Untyped content: map epoch values under time-named keys only, never
    inside a payload field."""
    if isinstance(data, dict):
        return {
            k: (
                v if k in PAYLOAD_FIELDS else None if _time_key(k) and is_epoch(v) else _loose(v, k)
            )
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_loose(v, key) for v in data]
    return data


def _admits_none(annotation: Any) -> bool:
    if annotation is Any or annotation is None or annotation is type(None):
        return True
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return type(None) in typing.get_args(annotation)
    return False


def _models_in(annotation: Any) -> tuple[type[BaseModel], ...]:
    out: list[type[BaseModel]] = []
    stack = [annotation]
    while stack:
        t = stack.pop()
        if isinstance(t, type) and issubclass(t, BaseModel):
            out.append(t)
            continue
        stack.extend(typing.get_args(t))
    return tuple(out)


def _untyped(annotation: Any) -> bool:
    """dict / list / Any content with no model inside it."""
    if annotation is Any:
        return True
    origin = typing.get_origin(annotation)
    if origin in (dict, list, typing.Union, types.UnionType) or annotation in (dict, list):
        return not _models_in(annotation)
    return False


_PlanRow = tuple[str, bool, tuple[type[BaseModel], ...], bool]


@lru_cache(maxsize=4096)
def _plan(model: type[BaseModel]) -> tuple[_PlanRow, ...]:
    """Per field of `model` (by its serialized name): may it carry null, the
    models nested in it, and whether its content is untyped."""
    rows = []
    for name, info in model.model_fields.items():
        key = info.serialization_alias or info.alias or name
        ann = info.annotation
        rows.append((key, _admits_none(ann), _models_in(ann), _untyped(ann)))
    return tuple(rows)


def scrub(model: Any, data: Any) -> Any:
    """`data` (a dumped `model`) with epoch values nulled where the model
    allows it. Anything that is not a dict dumped from a model passes through."""
    if not isinstance(data, dict) or not isinstance(model, type):
        return data
    if not issubclass(model, BaseModel):
        return data
    out = dict(data)
    for key, nullable, nested, untyped in _plan(model):
        if key not in out or key in PAYLOAD_FIELDS:
            continue
        value = out[key]
        if nullable and is_epoch(value):
            out[key] = None
        elif nested:
            out[key] = _into(nested, value)
        elif untyped:
            out[key] = _loose(value, key)
    return out


def _into(models: tuple[type[BaseModel], ...], value: Any) -> Any:
    if isinstance(value, list):
        return [_into(models, v) for v in value]
    if isinstance(value, dict):
        if len(models) == 1:
            return scrub(models[0], value)
        # A union of models: scrub against the one whose fields the dict
        # carries most of (a dumped model carries all of its own).
        best = max(models, key=lambda m: len(set(m.model_fields) & set(value)))
        return scrub(best, value)
    return value
