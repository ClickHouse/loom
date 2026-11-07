"""scripts/codegen/wire_changelog.py's registry probe runs against a real
revision: the release notes' wire diff depends on it."""

from __future__ import annotations

import importlib.util
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


def test_the_probe_reads_this_revisions_registry() -> None:
    """HEAD's registry, as committed."""
    snap = _module().snapshot_at("HEAD", repo=ROOT)
    assert {"memory.set", "memory.search", "memory.get", "code.search"} <= set(snap["ops"])
    assert all(op["path"] == f"/v1/{name}" for name, op in snap["ops"].items())
    assert "memory_key" in snap["ops"]["memory.set"]["request_fields"]
