"""A refused value insert publishes no searchable text."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.core.lease_writes import FenceUnavailableError
from server.memory import store
from server.memory.app import bulk_set, set_commit, update
from server.memory.store.rows import identity_key
from server.memory.store.write_fence import lease_name
from tests.unit.test_memory_verify import _row


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set", "update", "update_retire", "bulk_set"])
async def test_contended_value_write_publishes_no_embedding(
    operation, memory_insert_keeper, monkeypatch
):
    source = _row()
    memory = store.build_memory_version_row(source)
    embedding = {"memory_key": source.memory_key, "excerpt": "uncommitted value"}
    inserted: dict[str, list[dict[str, Any]]] = {}
    retire = AsyncMock()
    monkeypatch.setattr(store, "tombstone_vectors_of", retire)

    class CH:
        async def insert(self, table, rows, **kw):
            inserted.setdefault(table, []).extend(rows)

    memory_insert_keeper.seed(lease_name(identity_key(source)), "another-writer")
    ch = CH()
    with pytest.raises(FenceUnavailableError):
        if operation == "set":
            await set_commit._insert_row_and_vector(
                SimpleNamespace(memory_row=memory, embedder=SimpleNamespace(dim=4)), ch, embedding
            )
        elif operation in ("update", "update_retire"):
            await update._land_inserted(
                source,
                memory,
                [] if operation == "update_retire" else [embedding],
                ch,
                retire_vectors=operation == "update_retire",
                high=False,
                record=AsyncMock(),
            )
        else:
            await bulk_set._insert_batch(
                SimpleNamespace(memory_rows=[memory], embedding_rows=[embedding]), ch
            )
    assert inserted == {}, "a refused write must never be searchable"
    retire.assert_not_awaited()
