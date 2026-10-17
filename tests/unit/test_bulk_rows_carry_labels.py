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
    state = _BulkWriteState()
    await _bulk_existing_lookup(None, inp, state)
    await _bulk_build_rows(inp, state, {}, {}, NOW)
    await _bulk_embed_rows(inp, state, _Embedder(), {}, NS(mark=lambda _s: None), NOW)
    return state


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
    from server.memory.app.bulk_rows import settle_batch_rows

    value = "Alice lives in Berlin"
    state = await _build(
        monkeypatch,
        _stored(value),
        {"memory_key": "k", "value": value, "sensitivity": "high"},
        {"memory_key": "k", "value": value, "entities": ["Alice"]},
    )
    replay = settle_batch_rows(state, {0: NS(verdict="merge", existing_memory_id="X")})
    assert replay.drop == {1}


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
