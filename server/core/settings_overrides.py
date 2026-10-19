"""Deployment-wide overrides of LIVE settings, and where each value in force came from.

`loom settings set` and `/v1/admin/settings.set` store an override as one
loom.runtime_overrides row (kind `setting`, key the field name). Every process
lays the stored values over its own settings on each refresh (`adopt`), so an
override binds the whole deployment within `runtime_overrides_refresh_seconds`
and outlives a restart. Only a LIVE field takes one (`server.settings._live`);
clearing it puts back the value the process started with, from its environment
or the default. A build that no longer has the field, or no longer reads it
live, logs the row once and ignores it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from server.core import runtime_overrides as ro
from server.core.env_file import env_file_path
from server.core.errors import ValidationError
from server.core.repo_base import _CHClient
from server.settings import Settings, settings
from server.settings._live import coerce, is_live, to_json

log = logging.getLogger(__name__)

KIND = ro.SETTING

# field -> the value this process held before its first override of it.
_started_with: dict[str, Any] = {}
# field -> the override in force in this process.
_applied: dict[str, Any] = {}
# Stored rows already reported as unusable, said once each.
_warned: set[tuple[str, str]] = set()


def check(name: str, value: Any) -> Any:
    """`value` validated for LIVE field `name`; ValidationError otherwise."""
    field = Settings.model_fields.get(name)
    if field is None:
        raise ValidationError(f"no setting is named {name!r}")
    if not is_live(field):
        raise ValidationError(
            f"{name} is read once at startup: set LOOM_{name.upper()} and restart instead"
        )
    try:
        return coerce(Settings, name, value)
    except PydanticValidationError as e:
        raise ValidationError(f"{name}: {e.errors()[0]['msg']}, not {value!r}") from e


async def put(name: str, value: Any, *, ch: _CHClient | None = None) -> Any:
    """Store and apply an override; returns the value as the setting holds it.
    Stored first: a failed write changes no process."""
    checked = check(name, value)
    await ro.put(KIND, (name,), to_json(Settings, name, checked), ch=ch)
    _apply(name, checked)
    return checked


async def clear(name: str, *, ch: _CHClient | None = None) -> None:
    if name not in Settings.model_fields:
        raise ValidationError(f"no setting is named {name!r}")
    await ro.clear(KIND, (name,), ch=ch)
    _restore(name)


def adopt(snapshot: ro.Snapshot) -> None:
    """Take the deployment's setting overrides (a `runtime_overrides.load()` snapshot)."""
    wanted: dict[str, Any] = {}
    for key, raw in snapshot.get(KIND, {}).items():
        name = key[0] if len(key) == 1 else repr(key)
        try:
            wanted[name] = check(name, raw)
        except ValidationError as e:
            if (name, repr(raw)) not in _warned:
                _warned.add((name, repr(raw)))
                log.warning("setting override %s ignored: %s", name, e.message)
    for name, value in wanted.items():
        _apply(name, value)
    for name in _applied.keys() - wanted.keys():
        _restore(name)


def _apply(name: str, value: Any) -> None:
    if name not in _applied:
        _started_with[name] = getattr(settings, name)
    _applied[name] = value
    setattr(settings, name, value)


def _restore(name: str) -> None:
    if name in _applied:
        del _applied[name]
        setattr(settings, name, _started_with.pop(name))


def _sources() -> dict[str, str]:
    """field -> where its value in force came from: override, env, env_file or
    default. The environment wins over the dotenv file, as at startup."""
    environ = {k.upper() for k in os.environ}
    dotenv: set[str] = set()
    path = env_file_path()
    if path and os.path.isfile(path):
        from dotenv import dotenv_values

        dotenv = {k.upper() for k in dotenv_values(path)}
    out = {}
    for name in Settings.model_fields:
        env = f"LOOM_{name.upper()}"
        if name in _applied:
            out[name] = "override"
        elif env in environ:
            out[name] = "env"
        elif env in dotenv:
            out[name] = "env_file"
        else:
            out[name] = "default"
    return out


def _shown(name: str, value: Any) -> Any:
    """JSON for the listing; a secret shows only its length (`Settings._mask`)."""
    masked = settings._mask(name, value)
    return masked if masked is not value else to_json(Settings, name, value)


def rows(*, prefix: str = "", changed: bool = False) -> list[dict[str, Any]]:
    """Every setting whose name starts with `prefix`: its value in force, its
    default, where the value came from and whether it takes an override.
    `changed` keeps the ones whose value is not the default."""
    from server.settings._describe import descriptions

    about = descriptions(Settings)
    sources = _sources()
    out = []
    for name, field in Settings.model_fields.items():
        if not name.startswith(prefix):
            continue
        value = getattr(settings, name)
        default = field.get_default(call_default_factory=True)
        if changed and value == default:
            continue
        out.append(
            {
                "name": name,
                "env": f"LOOM_{name.upper()}",
                "value": _shown(name, value),
                "default": _shown(name, default),
                "source": sources[name],
                "live": is_live(field),
                "description": about.get(name, ""),
            }
        )
    return out


def row(name: str) -> dict[str, Any]:
    """The `rows()` entry of setting `name`; ValidationError when there is none."""
    if name not in Settings.model_fields:
        raise ValidationError(f"no setting is named {name!r}")
    return next(r for r in rows(prefix=name) if r["name"] == name)
