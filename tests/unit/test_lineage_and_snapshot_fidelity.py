"""Derived rows name their sources; a restore keeps the attestation
. Source-level checks — the writers are async CH paths."""

from __future__ import annotations

import re
from pathlib import Path

from server.identity.api.models import _SnapshotMemoryRow

ROOT = Path(__file__).resolve().parents[2]

ATTESTATION = {
    "version",
    "content_sha256",
    "actor",
    "source_class",
    "source_resource_id",
    "verified_by",
    "verified_at",
    "verified_sha256",
    "grounding",
    "evidence",
    "tags",
    "valid_at",
}


def test_snapshot_row_carries_attestation_and_provenance() -> None:
    fields = set(_SnapshotMemoryRow.model_fields)
    assert ATTESTATION <= fields, sorted(ATTESTATION - fields)
    row = _SnapshotMemoryRow(
        memory_id="55555555-5555-5555-5555-555555555555",
        agent="a",
        user_id="-",
        session_id="-",
        run_id="-",
        memory_key="k",
        memory_type="semantic",
        value='"v"',
        confidence=1.0,
        source_trace="",
        pinned=False,
    )
    # defaults are the "unknown" values a payload without lineage would carry
    assert row.version == 0 and row.content_sha256 == "" and row.verified_by == ""


def test_snapshot_query_and_restore_row_agree_with_the_model() -> None:
    """The snapshot SELECT and the restore row are both derived
    from MEMORY_PERSISTED_COLS, so every column the payload model carries is
    read and written back — the attestation set included."""
    from server.memory.app.snapshot_rows import SNAPSHOT_COLUMNS, SNAPSHOT_SELECT, restore_rows
    from server.memory.store import MEMORY_PERSISTED_COLS

    selected = set(SNAPSHOT_COLUMNS)
    assert ATTESTATION <= selected, sorted(ATTESTATION - selected)
    assert selected == set(MEMORY_PERSISTED_COLS) - {"org", "namespace", "memory_id"}
    # a snapshot reads live rows only, so the payload has no tombstoned_at
    payload_fields = set(_SnapshotMemoryRow.model_fields) | {"tombstoned_at"}
    assert payload_fields >= selected, sorted(selected - payload_fields)
    for col in selected:
        assert re.search(rf"\b{col}\b", SNAPSHOT_SELECT), col
    row = _SnapshotMemoryRow(
        memory_id="55555555-5555-5555-5555-555555555555",
        agent="a",
        user_id="-",
        session_id="-",
        run_id="-",
        memory_key="k",
        memory_type="semantic",
        value='"v"',
        confidence=1.0,
        source_trace="",
        pinned=False,
        verified_by="human:alice",
        verified_sha256="ab",
        grounding="quoted",
        evidence="q",
        valid_at=None,
        content_sha256="ab" * 32,
        tags=["t"],
    )
    (out,) = restore_rows(
        [row],
        org="o",
        target_namespace="n",
        rebind_agent=None,
        version_base=7,
        actor="process:restore",
    )
    assert set(out) == set(MEMORY_PERSISTED_COLS)
    for col in ("verified_by", "verified_sha256", "grounding", "evidence", "valid_at"):
        assert out[col] == getattr(row, col), col
    assert out["content_sha256"] == "ab" * 32 and out["tags"] == ["t"] and out["version"] == 7


def test_consolidation_writer_links_derived_from() -> None:
    src = (ROOT / "server/memory/app/consolidate.py").read_text()
    assert 'support_type="derived_from"' in src
    assert "link_belief_sources(" in src


def test_session_summary_records_the_keys_it_compressed() -> None:
    src = (ROOT / "server/memory/app/session_summary.py").read_text()
    assert "derived_from=" in src
    schema = (ROOT / "schemas" / "schema.sql").read_text()
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS loom.session_summaries") :]
    block = block[: block.index("ENGINE")]
    assert "`derived_from` Array(String) DEFAULT []" in block
