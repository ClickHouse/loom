"""Mirror-every-column discipline tests for the bi-temporal `valid_to` slot.

* `valid_to` exists on loom.memory_embeddings and is
  mirrored on every RMT write path (build_*_row + projections).
"""

from __future__ import annotations

import datetime

from server.memory.store import (
    EMBEDDING_PERSISTED_COLS,
    MEMORY_PERSISTED_COLS,
    build_embedding_row,
    build_memory_version_row,
)

# --------------------------------------------------------------------------
# valid_to on the read-side projection + mirror-every-column discipline
# --------------------------------------------------------------------------


def test_valid_to_present_on_embedding_mirror_cols() -> None:
    """The read-side denormalized table loom.memory_embeddings must carry
    valid_to so the read filter and rank can reference e.valid_to.
    The mirror funnel (build_embedding_row) is the single source of truth."""
    assert "valid_to" in EMBEDDING_PERSISTED_COLS
    row = build_embedding_row(org="o", namespace="n", memory_key="k")
    # Default NULL = open interval.
    assert row["valid_to"] is None


def test_valid_to_mirrored_on_memory_version_row() -> None:
    """loom.memory (the write source) also carries valid_to, and
    build_memory_version_row carries it forward verbatim on every RMT version
    write (omitting it would zero a write-side-closed interval under FINAL)."""
    assert "valid_to" in MEMORY_PERSISTED_COLS

    class _Existing:
        pass

    existing = _Existing()
    for col in MEMORY_PERSISTED_COLS:
        setattr(existing, col, None)
    closed = datetime.datetime(2024, 5, 1)
    existing.valid_to = closed  # type: ignore[attr-defined]
    row = build_memory_version_row(existing, status="superseded")  # type: ignore[arg-type]
    assert row["valid_to"] == closed


def test_embedding_row_can_carry_explicit_valid_to() -> None:
    """A write-side close (sibling extraction change, or a contradiction
    detector) can pass valid_to through the funnel — the column accepts it."""
    closed = datetime.datetime(2024, 5, 1)
    row = build_embedding_row(org="o", namespace="n", memory_key="k", valid_to=closed)
    assert row["valid_to"] == closed
