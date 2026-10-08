"""The loom.entity_summaries row builder mirrors every persisted column (a
missing column would silently zero under RMT(version) FINAL), and the read
leg stays off by default."""

from __future__ import annotations

from datetime import datetime

import pytest

from server.memory.store import ENTITY_SUMMARY_PERSISTED_COLS, build_entity_summary_row


def test_build_entity_summary_row_mirror_every_column() -> None:
    row = build_entity_summary_row(
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        entity="bowie",
        summary_text="Bowie, age 7, switched to Acana on 2024-03-11.",
        summary_embedding=[0.1, 0.2, 0.3, 0.4],
        embedding_model="fake-ngram-1024",
        embedding_dim=4,
        key_facts=["User has a dog named Bowie", "Bowie eats Acana"],
        fact_count=2,
        valid_from=None,
        version=123,
    )
    # MIRROR DISCIPLINE: every persisted column present. A missing column would
    # silently zero under ReplacingMergeTree FINAL.
    missing = [c for c in ENTITY_SUMMARY_PERSISTED_COLS if c not in row]
    assert not missing, f"loom.entity_summaries row missing columns: {missing}"
    assert set(row) == set(ENTITY_SUMMARY_PERSISTED_COLS)
    assert row["entity"] == "bowie"
    assert row["embedding_dim"] == 4
    assert row["fact_count"] == 2
    assert row["summary_embedding"] == [0.1, 0.2, 0.3, 0.4]
    # Non-nullable DateTimes filled even when unset.
    assert isinstance(row["created_at"], datetime)
    assert isinstance(row["updated_at"], datetime)
    assert row["version"] == 123


def test_build_entity_summary_row_rejects_unknown_column() -> None:
    # The canonical repo builder rejects a typo'd column name (a bug, not a
    # silently dropped value) — same discipline as build_session_summary_row.
    from server.memory.store import build_entity_summary_row

    with pytest.raises(ValueError):
        build_entity_summary_row(org="o", embeddings=[1, 2, 3])  # note the typo
