"""With `memory_temporal_enabled` off the write paths write what they wrote before
the journal existed: no journal-only columns (migration 039 never ran), and the
sensitivity label appended rather than put in place of another."""

from __future__ import annotations

from datetime import datetime

import pytest

from server.settings import settings

_JOURNAL_LEDGER_COLS = {
    "returned_ids",
    "returned_sha256",
    "cutoff_seq",
    "valid_as_of",
    "known_as_of",
}


@pytest.mark.parametrize("journal", [False, True])
def test_the_level_label_is_appended_without_the_journal(
    journal: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.feature_modes import temporal_on
    from server.memory.domain.sensitivity import with_level_label

    monkeypatch.setattr(settings, "memory_temporal_enabled", journal)
    got = with_level_label(["x", "sensitivity:medium"], "high", in_place=temporal_on())
    if journal:
        assert got == ["x", "sensitivity:high"]
    else:
        assert got == ["x", "sensitivity:medium", "sensitivity:high"]


@pytest.mark.parametrize("journal", [False, True])
def test_the_exposure_row_carries_the_receipt_columns_only_with_the_journal(
    journal: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app.exposure_ledger import build_row

    monkeypatch.setattr(settings, "memory_temporal_enabled", journal)
    row = build_row(operation="memory.get", org="o", namespace="n", keys=["k"])
    assert (_JOURNAL_LEDGER_COLS <= set(row)) is journal
    assert journal or not (_JOURNAL_LEDGER_COLS & set(row))


@pytest.mark.parametrize("journal", [False, True])
def test_graph_rows_carry_knowledge_time_only_with_the_journal(
    journal: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import projections

    monkeypatch.setattr(settings, "memory_temporal_enabled", journal)
    row = projections.entity_triple_row(
        "o",
        "n",
        "s",
        "r",
        "o2",
        provenance="p",
        confidence=0.5,
        adapter_name="a",
        last_seen=datetime(2026, 1, 1),
    )
    assert ("recorded_at" in row) is journal
    assert ("closed_at" in projections._closed()) is journal
