"""Each row of the worker registry names real settings, and a worker that is
switched off is never imported.

`test_worker_registry_snapshot` pins the specs the rows build. These tests
pin the rows themselves: the switch and cadence names are Settings fields of
the right kind (a string that names nothing would only fail when the worker
is enabled), and `enabled_workers()` imports only what it runs, which keeps
boot fast and a disabled worker's optional dependencies optional.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server.settings import Settings
from server.workers import registry

ROOT = Path(__file__).resolve().parents[2]
_FIELDS = Settings.model_fields
_ROWS = registry._WORKERS


def test_worker_names_are_unique() -> None:
    names = [w.name for w in _ROWS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("row", _ROWS, ids=[w.name for w in _ROWS])
def test_a_rows_switches_are_boolean_settings(row: registry._Worker) -> None:
    for switch in (row.enabled, row.also_requires):
        if switch is not None:
            assert switch in _FIELDS and _FIELDS[switch].annotation is bool, switch


@pytest.mark.parametrize("row", _ROWS, ids=[w.name for w in _ROWS])
def test_a_rows_cadences_are_numeric_settings(row: registry._Worker) -> None:
    names = [row.idle_interval, row.max_pass_setting]
    if not row.interval_in_module:
        names.append(row.interval)
    for name in (n for n in names if n is not None):
        assert name in _FIELDS and _FIELDS[name].annotation in (int, float), name


def test_only_the_always_on_worker_runs_with_every_switch_off() -> None:
    """A fresh interpreter, so a module another test imported cannot mask an
    eager import."""
    probe = """
import json, sys
from server.settings import Settings, settings
from server.workers import registry
for name, field in Settings.model_fields.items():
    if name.endswith("_enabled") and field.annotation is bool:
        setattr(settings, name, False)
before = set(sys.modules)
specs = registry.enabled_workers()
imported = set(sys.modules) - before
print(json.dumps({
    "specs": [s.name for s in specs],
    "always_on": [w.name for w in registry._WORKERS if w.enabled is None],
    "imported_switched_off": sorted(
        w.module for w in registry._WORKERS if w.enabled is not None and w.module in imported
    ),
    "preloaded": sorted(w.module for w in registry._WORKERS if w.module in before),
}))
"""
    env = {"PATH": os.environ.get("PATH", ""), "LOOM_ENV_FILE": "", "PYTHONPATH": str(ROOT)}
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60
    )
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["specs"] == got["always_on"]
    assert got["imported_switched_off"] == []
    assert got["preloaded"] == [], "importing the registry must not import a worker"


def _row(name: str) -> registry._Worker:
    return next(w for w in _ROWS if w.name == name)


def test_the_memory_expiry_sweep_row() -> None:
    row = _row("memory_expiry_sweep")
    assert row.module == "server.memory.workers.expiry_sweep"
    assert row.enabled == "memory_expiry_sweep_enabled"
    assert row.interval == "memory_expiry_sweep_interval_seconds"
    assert importlib.util.find_spec(row.module) is not None
