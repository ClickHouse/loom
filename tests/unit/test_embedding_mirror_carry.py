"""A re-embed carries every mirrored column.

memory_embeddings is ReplacingMergeTree(version): the worker's replacement
row wins under FINAL, so any column it omits reverts to the schema DEFAULT.
source_class and verified are ranking inputs, so both must be on the carry
list or re-embedded rows read a blank class.
"""

from __future__ import annotations

import re
from pathlib import Path

from server.memory.store import EMBEDDING_PERSISTED_COLS
from server.memory.workers.embedding import _carry_signals

ROOT = Path(__file__).resolve().parent.parent.parent

# Columns whose value must survive a re-embed byte-for-byte. Everything the
# ranking, the provenance gate and the conflict pass read from the row.
MIRRORED: tuple[str, ...] = (
    "source_class",
    "verified",
    "conflict_checked_at",
    "content_sha256",
    "status",
    "confidence",
    "reinforcement_count",
    "last_reinforced_at",
    "tags",
    "entities",
    "valid_at",
    "valid_to",
    "invalid_at",
    "expires_at",
    "temporal_anchor",
    "temporal_confidence",
    "source_resource_id",
    "files",
    "concepts",
    "created_at",
)


def _stub() -> dict[str, object]:
    return {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "scope": "private",
        "version": 10,
        "source_class": "extracted",
        "verified": 1,
        "conflict_checked_at": "2026-09-12 10:00:00",
        "content_sha256": "c" * 64,
        "status": "active",
        "confidence": 0.7,
        "reinforcement_count": 4,
    }


def test_carry_signals_keeps_provenance_attestation_and_conflict_stamp():
    carried = _carry_signals(_stub())
    assert carried["source_class"] == "extracted"
    assert carried["verified"] == 1
    assert carried["conflict_checked_at"] == "2026-09-12 10:00:00"
    assert carried["content_sha256"] == "c" * 64
    assert carried["status"] == "active" and carried["confidence"] == 0.7


def test_every_mirrored_column_is_carried_and_exists_on_the_row():
    carried = _carry_signals(_stub())
    missing = [c for c in MIRRORED if c not in carried]
    assert not missing, f"re-embed drops mirrored columns: {missing}"
    unknown = [c for c in MIRRORED if c not in EMBEDDING_PERSISTED_COLS]
    assert not unknown, f"not a memory_embeddings column: {unknown}"


def test_legacy_stub_without_the_columns_lands_the_defaults_not_a_crash():
    row = _stub()
    for c in ("source_class", "verified", "conflict_checked_at"):
        row.pop(c)
    carried = _carry_signals(row)
    assert carried["source_class"] == "" and carried["verified"] == 0
    assert carried["conflict_checked_at"] is None


def _function_body(fn_name: str) -> str:
    src = (ROOT / "server" / "memory" / "store" / "sweeps.py").read_text()
    i = src.index(f"async def {fn_name}")
    j = src.find("\nasync def ", i + 1)
    return src[i : j if j > 0 else len(src)]


def test_stub_and_orphan_selects_read_the_columns_the_carry_needs():
    # find_embedding_stubs' FINAL read of the stubs it hands the worker
    stub_select = _function_body("_stub_rows")
    for col in ("source_class", "verified", "conflict_checked_at", "content_sha256"):
        assert re.search(rf"\b{col}\b", stub_select), f"find_embedding_stubs omits {col}"
    assert "_stub_rows(" in _function_body("find_embedding_stubs")
    from server.memory.store.sweeps import _EMBEDDING_MIRROR_COLUMNS

    # the orphan repair and memory.reindex read loom.memory, which has no
    # verified column; the class and the attestation state must come across
    for fn in ("find_memories_without_embedding_rows", "find_memories_to_reindex"):
        assert "_EMBEDDING_MIRROR_COLUMNS" in _function_body(fn), fn
    assert "m.source_class" in _EMBEDDING_MIRROR_COLUMNS
    assert "m.content_sha256" in _EMBEDDING_MIRROR_COLUMNS
    assert re.search(r"\) AS verified\b", _EMBEDDING_MIRROR_COLUMNS)
