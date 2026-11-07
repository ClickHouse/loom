"""scripts/codegen/wire_changelog.py's registry probe reads the operation
registry where it lives: the release notes' wire diff depends on it. The probe
runs in process here; tests/integration/test_wire_changelog_snapshot.py runs it
against an archived revision."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "wire_changelog", ROOT / "scripts" / "codegen" / "wire_changelog.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_reads_the_registry() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(_module()._PROBE, {})
    ops = json.loads(out.getvalue())["ops"]
    assert {"memory.set", "memory.search", "memory.get", "code.search"} <= set(ops)
    assert all(op["path"] == f"/v1/{name}" for name, op in ops.items())
    assert "memory_key" in ops["memory.set"]["request_fields"]
