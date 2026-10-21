"""docs/reference/environment.md and `NON_SETTINGS_ENV` name the same LOOM_* variables.

`NON_SETTINGS_ENV` (server/settings/_env.py) is the list of LOOM_* variables that are
not settings fields, which the unknown-variable warning skips; the reference page
documents each. A name added to one and not the other fails here. Each family in
`NON_SETTINGS_ENV_PREFIXES` is documented as one `<PREFIX>...` row.
"""

from __future__ import annotations

import re
from pathlib import Path

from server.settings import Settings
from server.settings._env import NON_SETTINGS_ENV, NON_SETTINGS_ENV_PREFIXES

PAGE = Path(__file__).resolve().parents[2] / "docs" / "reference" / "environment.md"


def _documented() -> set[str]:
    """Every LOOM_* name in the first cell of a table row."""
    names: set[str] = set()
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        if line.startswith("| `"):
            names |= set(re.findall(r"`(LOOM_[A-Z0-9_]+)`", line.split("|")[1]))
    return names


def test_every_non_setting_variable_is_documented() -> None:
    missing = sorted(NON_SETTINGS_ENV - _documented())
    assert not missing, f"document these in {PAGE.name}: {missing}"


def test_every_documented_variable_is_known() -> None:
    fields = {f"LOOM_{f.upper()}" for f in Settings.model_fields}
    unknown = sorted(_documented() - NON_SETTINGS_ENV - fields)
    assert not unknown, (
        f"{PAGE.name} names variables that are neither settings nor in NON_SETTINGS_ENV: {unknown}"
    )


def test_every_non_setting_family_is_documented() -> None:
    lines = PAGE.read_text(encoding="utf-8").splitlines()
    missing = [
        p
        for p in NON_SETTINGS_ENV_PREFIXES
        if not any(line.startswith(f"| `{p}<") for line in lines)
    ]
    assert not missing, f"document a `<PREFIX>...` row in {PAGE.name} for: {missing}"
