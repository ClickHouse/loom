"""Deployment-wide overrides of LIVE settings, and where each value in force came from.

`loom settings set` and `/v1/admin/settings.set` store an override as one
loom.runtime_overrides row (kind `setting`, key the field name). Every process
lays the stored values over its own settings on each refresh (`adopt`), so an
override binds the whole deployment within `runtime_overrides_refresh_seconds`
and outlives a restart. Only a LIVE field takes one (`server.settings._live`);
clearing it puts back the value the process started with (from the environment,
the dotenv file, the settings file or the default). A build that no longer has
the field, no longer reads it live, or whose field refuses the stored value, logs the
row once and ignores it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from server.core import runtime_overrides as ro
from server.core.errors import LoomServerError, ValidationError
from server.core.repo_base import _CHClient
from server.settings import Settings, settings
from server.settings._env import is_secret_field
from server.settings._live import coerce, is_live, to_json

log = logging.getLogger(__name__)

KIND = ro.SETTING
# `change(expected=...)` not given: no compare-and-set.
NOT_GIVEN: Any = object()

# field -> the value this process held before its first override of it.
_started_with: dict[str, Any] = {}
# field -> the override in force in this process.
_applied: dict[str, Any] = {}
# Stored rows already reported as unusable, said once each.
_warned: set[tuple[str, str]] = set()
# Set by the first `adopt` in this process.
_adopted = False
# source -> the fields code outside this module set on `settings` (`register_source`).
_assigned: dict[str, Callable[[], Iterable[str]]] = {}


class SettingMovedError(LoomServerError):
    """409 — the setting's value in force is not the `expected` one the change
    named, so nothing changed; `current` carries the value in force."""

    code = "PRECONDITION_FAILED"
    status_code = 409


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


async def refresh(*, ch: _CHClient | None = None) -> ro.Snapshot:
    """Adopt the stored overrides now, not at the next refresh; returns what was read."""
    snapshot = await ro.load(ch=ch)
    adopt(snapshot)
    return snapshot


async def change(
    name: str,
    value: Any = None,
    *,
    clear: bool = False,
    expected: Any = NOT_GIVEN,
    ch: _CHClient | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Store (or, with `clear`, remove) the override of `name` and apply it here.
    Returns the `row()` before and after, None for a field this build does not
    have. Stored first: a failed write changes no process. Clearing takes any name
    with a stored override, a field since removed included, and refuses one with none.
    `expected`: refuse unless that is the value in force after a fresh read of the
    table, so a change made since through another replica or the CLI is not
    overwritten unseen; two changes in the same instant can still both pass (the
    table has no conditional write)."""
    snapshot = await refresh(ch=ch)
    known = name in Settings.model_fields
    before = row(name) if known else None
    previous = before["value"] if before else None
    if expected is not NOT_GIVEN and known:
        try:
            matches = coerce(Settings, name, expected) == getattr(settings, name)
        except PydanticValidationError:
            matches = False
        if not matches:
            raise SettingMovedError(
                f"{name} is {previous!r}, not the expected {expected!r}; nothing changed",
                current=previous,
            )
    if clear:
        if (name,) not in snapshot.get(KIND, {}):
            raise ValidationError(f"{name} has no override to reset")
        await ro.clear(KIND, (name,), ch=ch)
        _restore(name)
    else:
        checked = check(name, value)
        await ro.put(KIND, (name,), to_json(Settings, name, checked), ch=ch)
        _apply(name, checked)
    return before, row(name) if known else None


def adopted() -> bool:
    """Whether this process took the deployment's setting overrides once: before that, a
    LIVE setting reads the value the process started with, even where an operator
    overrode it. Readiness keeps its own flag (`boot._OVERRIDES_ADOPTED`): this one also
    turns on when an admin change refreshes the setting overrides alone, before the
    rate-limit and latency-budget ones were ever read."""
    return _adopted


def adopt(snapshot: ro.Snapshot) -> None:
    """Take the deployment's setting overrides (a `runtime_overrides.load()` snapshot)."""
    global _adopted
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
    _adopted = True


def _apply(name: str, value: Any) -> None:
    if name not in _applied:
        _started_with[name] = getattr(settings, name)
    _applied[name] = value
    setattr(settings, name, value)


def _restore(name: str) -> None:
    if name in _applied:
        del _applied[name]
        setattr(settings, name, _started_with.pop(name))


def register_source(source: str, fields: Callable[[], Iterable[str]]) -> None:
    """Code outside this module that sets fields on `settings` (the dashboard's
    model configuration) names them through `fields`, so the listing shows
    `source` for them."""
    _assigned[source] = fields


def _sources() -> dict[str, str]:
    """field -> where its value in force came from: override, dashboard, env,
    env_file, settings_file, inferred (filled in at load from other settings) or
    default, in the order startup ranks them, from what each source supplied
    when `settings` loaded."""
    by_code = {name: source for source, fields in _assigned.items() for name in fields()}
    # The CLI and compose copy the dotenv file into the environment, and scripts/serve.sh
    # the web tier's fields: those stay the file's.
    copied = settings.loaded_from("env_copied_from_env_file") | settings.loaded_from(
        "env_copied_from_settings_file"
    )
    env = settings.loaded_from("env") - copied
    out = {}
    for name, field in Settings.model_fields.items():
        if name in _applied:
            out[name] = "override"
        elif name in by_code:
            out[name] = by_code[name]
        elif name in env:
            out[name] = "env"
        elif name in settings.loaded_from("env_file"):
            out[name] = "env_file"
        elif name in settings.loaded_from("settings_file"):
            out[name] = "settings_file"
        # No source named it, yet it is not the default: load computed it from
        # other settings (the LLM provider from LOOM_LLM_MODEL, a local model's width).
        elif name in settings._inferred_fields or getattr(settings, name) != field.get_default(
            call_default_factory=True
        ):
            out[name] = "inferred"
        else:
            out[name] = "default"
    return out


def _shown(name: str, value: Any) -> Any:
    """JSON for the listing; a set secret shows only its length (`Settings._mask`)."""
    return settings._mask(name, value) if is_secret_field(name) else to_json(Settings, name, value)


def rows(*, prefix: str = "", changed: bool = False) -> list[dict[str, Any]]:
    """Every setting whose name starts with `prefix`: its value in force, its
    default, where the value came from and whether it takes an override.
    `changed` keeps the ones off their default or overridden."""
    from server.settings._describe import descriptions

    about = descriptions(Settings)
    sources = _sources()
    out = []
    for name, field in Settings.model_fields.items():
        if not name.startswith(prefix):
            continue
        value = getattr(settings, name)
        default = field.get_default(call_default_factory=True)
        if changed and value == default and sources[name] != "override":
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
