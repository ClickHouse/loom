"""Wire fields that belong to one side of `memory_bitemporal_enabled`.

A field marked `BITEMPORAL` exists only for the bi-temporal journal; one marked
`CLASSIC` is the field the journal replaces (single-axis `as_of`, the version-row
rollback's counters). The transports treat a field of the inactive side as one
the op does not have: an input is reset to its default and named in
`ignored_args`, an output key is left out, and the MCP listing omits it. So with
the journal off the wire is what it was before the journal existed, and with it
on the replaced fields are ignored as before.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from server.settings import settings

BITEMPORAL: dict[str, Any] = {"loom_mode": "bitemporal"}
CLASSIC: dict[str, Any] = {"loom_mode": "classic"}


def bitemporal_on() -> bool:
    return settings.memory_bitemporal_enabled


def _mode(info: Any) -> str | None:
    extra = info.json_schema_extra
    return extra.get("loom_mode") if isinstance(extra, dict) else None


def inactive_fields(model: type[BaseModel]) -> frozenset[str]:
    """The model's fields (by name and alias) of the side that is off now."""
    off = "classic" if bitemporal_on() else "bitemporal"
    names: set[str] = set()
    for name, info in model.model_fields.items():
        if _mode(info) == off:
            names.add(name)
            if info.alias:
                names.add(info.alias)
    return frozenset(names)


def strip_inactive_input(model: BaseModel) -> list[str]:
    """Reset the inactive side's fields a request set to their defaults; the names
    reset, for `ignored_args`."""
    off = inactive_fields(type(model))
    reset = sorted(n for n in model.model_fields_set if n in off)
    for name in reset:
        info = type(model).model_fields[name]
        object.__setattr__(model, name, info.get_default(call_default_factory=True))
        model.model_fields_set.discard(name)
    return reset


def drop_inactive_output(model: type[BaseModel] | None, body: Any) -> Any:
    """`body` (a dumped `model`) without the inactive side's keys, nested models
    and lists of them included."""
    if model is None or not isinstance(body, dict):
        return body
    off = inactive_fields(model)
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key in off:
            continue
        info = model.model_fields.get(key)
        out[key] = _nested(info.annotation if info is not None else None, value)
    return out


def _nested(annotation: Any, value: Any) -> Any:
    sub = _model_in(annotation)
    if sub is None:
        return value
    if isinstance(value, list):
        return [drop_inactive_output(sub, v) for v in value]
    return drop_inactive_output(sub, value)


def _model_in(annotation: Any) -> type[BaseModel] | None:
    """The BaseModel an annotation holds (`M`, `M | None`, `list[M]`), if any."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in getattr(annotation, "__args__", ()) or ():
        found = _model_in(arg)
        if found is not None:
            return found
    return None
