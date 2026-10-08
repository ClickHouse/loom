"""Unit tests for build_embedding_row — the single source of truth for
loom.memory_embeddings INSERT rows. Guards the silent-data-loss class where a
write path omits a column and FINAL later surfaces the schema default over a
prior real value.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from server.memory.store import (
    EMBEDDING_PERSISTED_COLS,
    build_embedding_row,
)


def test_builder_emits_every_persisted_column() -> None:
    row = build_embedding_row(org="o", namespace="n", memory_key="k")
    # No column may be silently missing — that is the whole point.
    assert set(row) == set(EMBEDDING_PERSISTED_COLS)


def test_non_nullable_datetimes_are_never_none() -> None:
    row = build_embedding_row(org="o")
    # created_at and last_reinforced_at are non-Nullable DateTime in the DDL;
    # the builder must fill them so an insert never sends NULL.
    assert isinstance(row["created_at"], datetime)
    assert isinstance(row["last_reinforced_at"], datetime)


def test_overrides_are_applied() -> None:
    row = build_embedding_row(
        org="o", memory_key_embedding=[0.1, 0.2], tags=["a"], reinforcement_count=5
    )
    assert row["org"] == "o"
    assert row["memory_key_embedding"] == [0.1, 0.2]
    assert row["tags"] == ["a"]
    assert row["reinforcement_count"] == 5


def test_defaults_match_schema_for_omitted_signal_columns() -> None:
    row = build_embedding_row(org="o")
    # A fresh insert that doesn't supply these gets safe defaults (same as the
    # DDL DEFAULTs) rather than an absent key.
    assert row["memory_key_embedding"] == []
    assert row["entities"] == []
    assert row["tags"] == []
    assert row["status"] == "active"
    assert row["scope"] == "private"
    assert row["reinforcement_count"] == 1


def test_unknown_column_raises() -> None:
    # A typo'd column name is a bug, not a silently dropped value.
    with pytest.raises(ValueError, match="unknown columns"):
        build_embedding_row(org="o", embeddings=[1, 2, 3])  # note the typo
