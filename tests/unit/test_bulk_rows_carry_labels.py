"""What a memory.bulk_set entry keeps from the stored row under its key: its label,
its expiry and its lock, as memory.set would. The rows are built and embedded with
the store read and the embedder stubbed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.core.hashing import sha256_value
from server.memory.api.models_bulk import MemoryBulkSetInput
from server.memory.store import MemoryRow

NOW = datetime.now(UTC)
STORED_ID = "11111111-1111-1111-1111-111111111111"


def _stored(value: str = "Alice lives in Berlin", **cols: Any) -> MemoryRow:
    """The live row under key k."""
    base: dict[str, Any] = {
        "memory_id": STORED_ID,
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": value,
        "content_sha256": sha256_value(value),
        "version": 1,
        "confidence": 1.0,
        "pinned": False,
        "expires_at": None,
        "created_at": (NOW - timedelta(days=3)).replace(tzinfo=None),
    }
    return MemoryRow(**{**base, **cols})


class _Embedder:
    model_name = "fake"
    dim = 2

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


async def _build(monkeypatch: pytest.MonkeyPatch, stored: MemoryRow | None, *entries: dict) -> Any:
    """The state after one bulk of `entries` over `stored` is built and embedded."""
    state, _emb_idx_of = await _build_embedded(monkeypatch, stored, *entries)
    return state


async def _build_embedded(
    monkeypatch: pytest.MonkeyPatch, stored: MemoryRow | None, *entries: dict
) -> Any:
    """The state and the embedding rows' memory-row indexes after one bulk of `entries`
    over `stored` is built and embedded."""
    import server.memory.store as memory_repo
    from server.memory.app import blacklist_guard
    from server.memory.app.bulk_rows import _bulk_build_rows, _bulk_embed_rows, _BulkWriteState
    from server.memory.app.bulk_set import _bulk_existing_lookup

    async def no_match(**_kw: Any) -> None:
        return None

    async def live_rows(idents: list[tuple], **_kw: Any) -> dict:
        return {i: stored for i in idents if stored is not None and i[5] == stored.memory_key}

    monkeypatch.setattr(blacklist_guard, "find_match", no_match)
    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", live_rows)
    inp = MemoryBulkSetInput(org="o", namespace="n", agent="a", entries=list(entries))
    state = _BulkWriteState(now_utc=NOW)
    await _bulk_existing_lookup(None, inp, state)
    await _bulk_build_rows(inp, state, {}, {})
    emb_idx_of, _vectors, _entities = await _bulk_embed_rows(
        inp, state, _Embedder(), {}, NS(mark=lambda _s: None)
    )
    return state, emb_idx_of


async def _settled(
    monkeypatch: pytest.MonkeyPatch, stored: MemoryRow | None, verdicts: dict, *entries: dict
) -> Any:
    """The state and verdict plan after one bulk of `entries` over `stored`, given the
    conflict `verdicts` (by built row)."""
    from server.memory.app import bulk_set

    async def given(*_a: Any, **_kw: Any) -> dict:
        return verdicts

    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", given)
    state, emb_idx_of = await _build_embedded(monkeypatch, stored, *entries)
    plan = await bulk_set._bulk_verdicts(None, state, emb_idx_of, [], NS(mark=lambda _s: None))
    return state, plan


async def test_the_embedding_row_carries_the_rows_label_tags(monkeypatch) -> None:
    # The ranking reads the embedding row's tags: it must hold the sensitivity tag the
    # memory row holds, as memory.set's does.
    state = await _build(
        monkeypatch,
        None,
        {"memory_key": "k", "value": "Alice lives in Berlin", "sensitivity": "medium"},
    )
    (row,), (emb,) = state.memory_rows, state.embedding_rows
    assert "sensitivity:medium" in row["tags"]
    assert emb["tags"] == row["tags"]


async def test_a_plain_repeat_of_a_high_row_keeps_it_high_and_unembedded(monkeypatch) -> None:
    # set_from_messages names no sensitivity: re-extracting a stored 'high' fact must not
    # write it again unlabelled, with a live vector.
    value = "Alice lives in Berlin"
    state = await _build(
        monkeypatch, _stored(value, sensitivity="high"), {"memory_key": "k", "value": value}
    )
    assert state.memory_rows == [] and state.embedding_rows == []
    assert state.skipped_unchanged == 1


async def test_a_repeat_that_names_no_level_inherits_the_label_it_repeats(monkeypatch) -> None:
    # A pin is a new label, so the row is written, but with the 'high' it repeats: the
    # stored row's, or an earlier entry's.
    value = "Alice lives in Berlin"
    pin = {"memory_key": "k", "value": value, "pinned": True}
    for stored, entries in (
        (_stored(value, sensitivity="high"), [pin]),
        (None, [{"memory_key": "k", "value": value, "sensitivity": "high"}, pin]),
    ):
        state = await _build(monkeypatch, stored, *entries)
        row = state.memory_rows[-1]
        assert row["pinned"] == 1 and row["sensitivity"] == "high"
        assert "sensitivity:high" in row["tags"]
        assert state.embedding_rows == []


async def test_a_level_the_entry_names_wins(monkeypatch) -> None:
    value = "Alice lives in Berlin"
    entry = {"memory_key": "k", "value": value, "sensitivity": "low"}
    state = await _build(monkeypatch, _stored(value, sensitivity="high"), entry)
    (row,) = state.memory_rows
    assert row["sensitivity"] == "low"


async def test_a_repeat_that_names_no_level_is_not_a_label_change(monkeypatch) -> None:
    # Entry 0 relabels the stored value 'high' but merges elsewhere, so the key keeps ''.
    # Entry 1 repeats the value naming no level: serially it is unchanged, whatever label
    # it took from entry 0's row when it was built.
    from server.memory.app.bulk_rows import UNCHANGED

    value = "Alice lives in Berlin"
    state, _plan = await _settled(
        monkeypatch,
        _stored(value),
        {0: NS(verdict="merge", existing_memory_id="X")},
        {"memory_key": "k", "value": value, "sensitivity": "high"},
        {"memory_key": "k", "value": value, "entities": ["Alice"]},
    )
    assert [s.outcome for s in state.steps] == ["merge", UNCHANGED]


async def test_a_repeat_of_the_stored_bytes_keeps_a_live_expiry(monkeypatch) -> None:
    # A pin on the same bytes writes a version; as on memory.set, the row keeps the live
    # expiry the stored row had. A lapsed one is not revived, and a new value keeps
    # bulk's rule (its own ttl_seconds, else none).
    value = "Alice lives in Berlin"
    soon = (NOW + timedelta(days=10)).replace(tzinfo=None, microsecond=0)
    past = (NOW - timedelta(days=1)).replace(tzinfo=None, microsecond=0)
    pin = {"memory_key": "k", "value": value, "pinned": True}
    for stored_expiry, entry, want in (
        (soon, pin, soon),
        (past, pin, None),
        (soon, {**pin, "value": "Alice lives in Paris"}, None),
    ):
        state = await _build(monkeypatch, _stored(value, expires_at=stored_expiry), entry)
        (row,) = state.memory_rows
        assert row["expires_at"] == want


async def test_a_repeat_on_a_read_only_row_never_changes_its_expiry(monkeypatch) -> None:
    # The lock refuses an expiry change (memory.set: 423, refused_action 'expire'). A new
    # label that leaves the expiry alone is written with the stored expiry; one whose
    # retention window or ttl_seconds would move it is refused as locked.
    from server.settings import settings

    value = "Alice lives in Berlin"
    month = (NOW + timedelta(days=30)).replace(tzinfo=None, microsecond=0)
    medium = {"memory_key": "k", "value": value, "sensitivity": "medium"}
    for window_days, entry, written in (
        (90, medium, True),
        (7, medium, False),
        (0, {"memory_key": "k", "value": value, "ttl_seconds": 60, "pinned": True}, False),
    ):
        monkeypatch.setattr(settings, "memory_sensitive_retention_days", window_days)
        stored = _stored(value, expires_at=month, lock_mode="read_only")
        state = await _build(monkeypatch, stored, entry)
        if written:
            (row,) = state.memory_rows
            assert row["expires_at"] == month and row["sensitivity"] == "medium"
        else:
            assert state.memory_rows == [] and state.skipped_locked == ["k"]
            assert state.results[0].conflict_verdict == "locked"


async def test_a_repeat_keeps_the_expiry_of_the_batch_row_it_repeats(monkeypatch) -> None:
    # Serially, the second memory.set meets the value the first one wrote: the same bytes
    # keep that row's expiry, and other bytes start with none, whatever the stored row had.
    from server.core.datetimes import to_naive_utc

    ten_days = (NOW + timedelta(days=10)).replace(tzinfo=None, microsecond=0)
    ttl_row = {"memory_key": "k", "value": "C", "ttl_seconds": 600}
    for stored, entries, want in (
        (
            None,
            [ttl_row, {"memory_key": "k", "value": "C", "pinned": True}],
            NOW + timedelta(seconds=600),
        ),
        (
            _stored("A", expires_at=ten_days),
            [{"memory_key": "k", "value": "C"}, {"memory_key": "k", "value": "A"}],
            None,
        ),
    ):
        state = await _build(monkeypatch, stored, *entries)
        assert to_naive_utc(state.memory_rows[-1]["expires_at"]) == to_naive_utc(want)


async def test_a_relabelling_repeat_still_reinforces_the_row(monkeypatch) -> None:
    # memory.set counts the same bytes as a restatement even when the write changes a
    # label: the row lands and is reinforced once.
    from server.memory.app.bulk_rows import RELABEL

    value = "Alice lives in Berlin"
    for entry in (
        {"memory_key": "k", "value": value, "sensitivity": "medium"},
        {"memory_key": "k", "value": value, "pinned": True},
    ):
        state, _plan = await _settled(monkeypatch, _stored(value), {}, entry)
        assert [s.outcome for s in state.steps] == [RELABEL]
        assert state.restated_ids == [STORED_ID]


async def test_a_ttl_only_repeat_on_a_read_only_row_is_locked(monkeypatch) -> None:
    # memory.set 423s any ttl_seconds on a read_only row, even with the same bytes and
    # labels; bulk answers that entry locked, not unchanged.
    value = "Alice lives in Berlin"
    stored = _stored(value, lock_mode="read_only")
    state = await _build(
        monkeypatch, stored, {"memory_key": "k", "value": value, "ttl_seconds": 60}
    )
    assert state.memory_rows == [] and state.skipped_locked == ["k"]
    assert state.skipped_unchanged == 0 and state.restated_ids == []
    assert state.results[0].conflict_verdict == "locked"


async def test_a_repeat_after_a_value_that_did_not_land_keeps_the_stored_labels(
    monkeypatch,
) -> None:
    # Stored k is 'high' with an expiry. Entry 0 writes another value and merges elsewhere,
    # so k keeps its row; entry 1 says the stored value again, naming no level. Serially it
    # is unchanged, and with a pin it lands still 'high', with the stored expiry, unembedded.
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitive_retention_days", 0)
    value = "Alice lives in Berlin"
    soon = (NOW + timedelta(days=10)).replace(tzinfo=None, microsecond=0)
    stored = _stored(value, sensitivity="high", expires_at=soon)
    other = {"memory_key": "k", "value": "Alice lives in Paris"}
    merge = {0: NS(verdict="merge", existing_memory_id="X", peers=[])}
    again = {"memory_key": "k", "value": value}
    state, plan = await _settled(monkeypatch, stored, merge, other, again)
    assert plan.memory_rows == [] and plan.embedding_rows == []
    assert [r.version for r in state.results] == [0, 0]
    assert state.skipped_unchanged == 1 and state.restated_ids == [STORED_ID]
    state, plan = await _settled(monkeypatch, stored, merge, other, {**again, "pinned": True})
    (row,) = plan.memory_rows
    assert row["sensitivity"] == "high" and "sensitivity:high" in row["tags"]
    assert row["expires_at"] == soon and row["pinned"] == 1
    assert plan.embedding_rows == []


async def test_a_repeat_of_a_batch_value_that_did_not_land_takes_no_labels_from_it(
    monkeypatch,
) -> None:
    # Entry 0 writes k with a ttl and a level, and is merged or refused; entry 1 writes the
    # same bytes with a pin. Serially entry 1 meets no row: no expiry, the detected level.
    from server.memory.domain.sensitivity import resolve_sensitivity
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitive_retention_days", 0)
    value = "Alice lives in Berlin"
    first = {"memory_key": "k", "value": value, "ttl_seconds": 600, "sensitivity": "medium"}
    pin = {"memory_key": "k", "value": value, "pinned": True}
    for verdict in ("merge", "rejected"):
        dropped = {0: NS(verdict=verdict, existing_memory_id="X", peers=[])}
        state, plan = await _settled(monkeypatch, None, dropped, first, pin)
        (row,) = plan.memory_rows
        assert row["expires_at"] is None and row["pinned"] == 1
        assert row["sensitivity"] == resolve_sensitivity("", f'"{value}"')
        assert "sensitivity:medium" not in row["tags"]
        (emb,) = plan.embedding_rows
        assert emb["tags"] == row["tags"] and emb["expires_at"] is None
        assert state.results[1].version == row["version"]


@pytest.mark.parametrize("vectorless", [True, False])
def test_a_row_landing_without_its_vector_retires_the_keys_old_one(vectorless: bool) -> None:
    # The replay can land an embeddable row with no embed call left: the old value's
    # vector would answer for the new value, so it retires.
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_set import _vectors_to_retire

    state = _BulkWriteState(now_utc=NOW, existing_lookup_ok=True)
    state.existing_rows["k"] = NS(memory_type="semantic", sensitivity="")
    state.memory_rows = [
        {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k",
            "memory_type": "semantic",
            "sensitivity": "",
            "version": 2,
        }
    ]
    if vectorless:
        state.vectorless_keys.add("k")
    assert ("k" in _vectors_to_retire(state)) is vectorless
