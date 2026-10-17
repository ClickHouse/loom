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
