"""scripts/codegen/wire_changelog.py's snapshot of a real revision: the
committed registry, archived and probed in a child interpreter, as the release
notes read the previous release."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.codegen import wire_changelog

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.slow


def test_the_snapshot_reads_this_revisions_registry() -> None:
    """HEAD's registry, as committed."""
    snap = wire_changelog.snapshot_at("HEAD", repo=ROOT)
    assert {"memory.set", "memory.search", "memory.get", "code.search"} <= set(snap["ops"])
    assert "memory_key" in snap["ops"]["memory.set"]["request_fields"]
