"""A setting's default changes only with an entry in server/settings/_history.py.

The snapshot pins each setting's default as it was when the setting was added.
Replaying DEFAULT_CHANGES over it must give today's defaults. A new or removed
setting updates the snapshot with LOOM_UPDATE_SNAPSHOT=1, which adds and drops
names but never rewrites a pinned value.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from server.settings import Settings
from server.settings._env import is_secret_field
from server.settings._history import DEFAULT_CHANGES
from server.settings._live import to_json

SNAPSHOT = Path(__file__).parent / "snapshots" / "settings_defaults.json"


def _defaults() -> dict[str, Any]:
    return {
        name: to_json(Settings, name, field.get_default(call_default_factory=True))
        for name, field in Settings.model_fields.items()
        if not is_secret_field(name)
    }


def _pinned() -> dict[str, Any]:
    pinned = json.loads(SNAPSHOT.read_text())
    if os.environ.get("LOOM_UPDATE_SNAPSHOT") == "1":
        current = _defaults()
        pinned = {n: pinned.get(n, _first_default(n, current[n])) for n in sorted(current)}
        SNAPSHOT.write_text(json.dumps(pinned, indent=1, sort_keys=True) + "\n")
    return pinned


def _first_default(name: str, current: Any) -> Any:
    """A setting new to the snapshot is pinned at the default it had before its
    first recorded change, or today's."""
    first = next((c for c in DEFAULT_CHANGES if c.name == name), None)
    return first.old if first else current


def test_every_default_is_pinned_or_has_a_recorded_change() -> None:
    expected = dict(_pinned())
    problems = []
    for change in DEFAULT_CHANGES:
        if change.name not in expected:
            continue  # a setting since removed
        if expected[change.name] != change.old:
            problems.append(
                f"{change.name}: the {change.version} entry says the default was "
                f"{change.old!r}, but it was {expected[change.name]!r}"
            )
        expected[change.name] = change.new
    current = _defaults()
    missing = sorted(current.keys() - expected.keys())
    gone = sorted(expected.keys() - current.keys())
    if missing or gone:
        problems.append(
            f"settings added {missing} or removed {gone}: run "
            f"LOOM_UPDATE_SNAPSHOT=1 pytest {Path(__file__).relative_to(Path.cwd())}"
        )
    for name in sorted(current.keys() & expected.keys()):
        if current[name] != expected[name]:
            problems.append(
                f"{name}: the default is {current[name]!r}, not {expected[name]!r}; record "
                "the change as a DefaultChange in server/settings/_history.py"
            )
    assert not problems, "\n".join(problems)


def test_each_change_names_a_release_and_a_reason() -> None:
    for change in DEFAULT_CHANGES:
        assert re.fullmatch(r"\d+\.\d+\.\d+", change.version), change
        assert change.name in Settings.model_fields or change.why, change
        assert change.old != change.new and change.why.strip(), change
