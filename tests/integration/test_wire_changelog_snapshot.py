"""scripts/codegen/wire_changelog.py's snapshot of a real revision: the
committed registry, archived and probed in a child interpreter, as the release
notes read the previous release."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.slow


def _module():
    spec = importlib.util.spec_from_file_location(
        "wire_changelog", ROOT / "scripts" / "codegen" / "wire_changelog.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_snapshot_reads_this_revisions_registry() -> None:
    """HEAD's registry, as committed."""
    snap = _module().snapshot_at("HEAD", repo=ROOT)
    assert {"memory.set", "memory.search", "memory.get", "code.search"} <= set(snap["ops"])
    assert "memory_key" in snap["ops"]["memory.set"]["request_fields"]
