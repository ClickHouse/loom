"""scripts/codegen/wire_changelog.py's registry probe reads the operation
registry where it lives: the release notes' wire diff depends on it. The probe
runs in process here; tests/integration/test_wire_changelog_snapshot.py runs it
against an archived revision."""

from __future__ import annotations

import contextlib
import io
import json

from scripts.codegen import wire_changelog


def test_the_probe_reads_the_registry() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(wire_changelog._PROBE, {})
    ops = json.loads(out.getvalue())["ops"]
    assert {"memory.set", "memory.search", "memory.get", "code.search"} <= set(ops)
    assert "memory_key" in ops["memory.set"]["request_fields"]
