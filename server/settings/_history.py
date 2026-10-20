"""Every change to a setting's default, with the release that ships it and why.

A default is behaviour: every deployment that never set the variable changes with
it. `tests/unit/test_settings_defaults.py` replays this list over the pinned
defaults in `tests/unit/snapshots/settings_defaults.json` and fails on a default
that changed without an entry here. The configuration reference lists them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DefaultChange:
    # The Loom release that ships the new default (pyproject.toml's version).
    version: str
    name: str
    old: Any
    new: Any
    why: str


# Oldest first.
DEFAULT_CHANGES: tuple[DefaultChange, ...] = (
    DefaultChange(
        version="1.0.0",
        name="namespace_autoregister_skip_prefixes",
        old="namespace.,code.,admin.,ops.,grants.,policy.,blacklist.,erasure.",
        new="namespace.,code.,admin.,ops.,grants.,policy.,blacklist.,erasure.,extraction_policy.",
        why="extraction_policy ops set an operator policy on a namespace, not data in it.",
    ),
)
