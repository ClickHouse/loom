"""The worker set `enabled_workers()` builds, pinned field by field.

`enabled_workers` turns settings into the supervised worker list: which
workers run, in which order, at which cadence, with which deadline, idle
sleep and first-pass hook. A refactor of how it is written must not move
any of that, so this test builds the list under three settings profiles and
compares every `WorkerSpec` field against `snapshots/worker_registry.json`:

  - ``defaults``: every worker switch and cadence at its declared default;
  - ``all_on``: every ``*_enabled`` flag on;
  - ``production``: the worker switches a production `.env` sets (booleans only,
    copied into `_PRODUCTION_FLAGS`), everything else at its default.

Callables are named by ``__module__:__qualname__``. A closure's qualname
carries the function it is defined in (``<fn>.<locals>.``), which is where
the code sits rather than what it runs, so that prefix is dropped; for a
callable defined in the registry module the fingerprint adds the names it
reads, its free variables and what those cells hold, so a config wrapper
that read a different setting or wrapped a different pass would differ.

Regenerate (only for an intended change to the worker set):
``LOOM_UPDATE_WORKER_SNAPSHOT=1 python -m pytest tests/unit/test_worker_registry_snapshot.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import types
from pathlib import Path
from typing import Any

import pytest

from server.settings import Settings, settings
from server.workers import registry

_SNAPSHOT = Path(__file__).parent / "snapshots" / "worker_registry.json"

# The LOOM_*_ENABLED worker switches a production deployment sets.
_PRODUCTION_FLAGS: dict[str, bool] = {
    "code_ingest_enabled": True,
    "code_embed_recovery_enabled": True,
    "code_scip_apply_enabled": True,
    "code_scip_go_delta_enabled": True,
    "code_scip_ts_delta_enabled": True,
    "code_graph_stats_enabled": True,
    "code_pr_history_enabled": True,
    "reflection_distill_enabled": True,
    "observe_extract_enabled": True,
    "entity_communities_enabled": True,
}


def _worker_fields() -> list[str]:
    """The settings the registry reads when it builds specs: every switch
    and every cadence / deadline."""
    return [name for name in Settings.model_fields if name.endswith(("_enabled", "_seconds"))]


def _default(name: str) -> Any:
    return Settings.model_fields[name].get_default(call_default_factory=True)


def _callable_id(fn: Any) -> str:
    qual = getattr(fn, "__qualname__", type(fn).__qualname__)
    return f"{getattr(fn, '__module__', '?')}:{qual.split('.<locals>.')[-1]}"


def _cell_id(value: Any) -> str:
    if callable(value):
        return _callable_id(value)
    return repr(value)


def _fingerprint(fn: Any) -> Any:
    if not callable(fn):
        return repr(fn)
    out: dict[str, Any] = {"id": _callable_id(fn)}
    code = getattr(fn, "__code__", None)
    if getattr(fn, "__module__", None) == registry.__name__ and isinstance(code, types.CodeType):
        out["names"] = list(code.co_names)
        out["freevars"] = list(code.co_freevars)
        out["cells"] = [_cell_id(c.cell_contents) for c in (fn.__closure__ or ())]
    return out


def _snapshot_specs() -> list[dict[str, Any]]:
    rows = []
    for spec in registry.enabled_workers():
        row: dict[str, Any] = {}
        for field in dataclasses.fields(spec):
            value = getattr(spec, field.name)
            if field.name in ("pass_callable", "first_pass_due", "due_scope"):
                row[field.name] = None if value is None else _fingerprint(value)
            elif field.name == "owned_tables":
                row[field.name] = [repr(t) for t in value]
            else:
                row[field.name] = value
        row["deadline_seconds()"] = spec.deadline_seconds()
        row["has_first_pass_hook"] = spec.first_pass_hook() is not None
        rows.append(row)
    return rows


def _profiles() -> dict[str, dict[str, Any]]:
    base = {name: _default(name) for name in _worker_fields()}
    all_on = {**base, **{n: True for n in base if n.endswith("_enabled")}}
    return {
        "defaults": base,
        "all_on": all_on,
        "production": {**base, **_PRODUCTION_FLAGS},
    }


def _build_all(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for label, values in _profiles().items():
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)
        out[label] = _snapshot_specs()
    return out


def test_production_flags_are_settings() -> None:
    for name in _PRODUCTION_FLAGS:
        assert name in Settings.model_fields, name


def test_enabled_workers_match_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _build_all(monkeypatch)
    if os.environ.get("LOOM_UPDATE_WORKER_SNAPSHOT") == "1":
        _SNAPSHOT.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(_SNAPSHOT.read_text())
    got = json.loads(json.dumps(got))
    assert sorted(got) == sorted(want)
    for label in want:
        assert [r["name"] for r in got[label]] == [r["name"] for r in want[label]], label
        assert got[label] == want[label], label
