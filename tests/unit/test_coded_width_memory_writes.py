"""A memory write whose vector a coded `memory_embeddings` column cannot take is
refused before its version row lands; a write that carries no vector is not."""

from __future__ import annotations

import pytest

from server.core.errors import CodedWidthMismatchError
from server.core.vectors import codes_state
from server.memory.api.models_crud import MemorySetInput, MemoryUpdateInput
from server.memory.app import update as update_mod
from server.memory.app import write as write_mod
from tests.unit.memory_store import FixedEmbedder, MemoryStore, wire_memory_store

IDENTITY = {"org": "o", "namespace": "n", "agent": "a"}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    store = MemoryStore()
    wire_memory_store(monkeypatch, store)
    monkeypatch.setattr(write_mod, "get_embedder", lambda: FixedEmbedder())
    return store


def _coded_at(monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    monkeypatch.setattr(codes_state, "_widths", {"loom.memory_embeddings.embedding": width})


@pytest.mark.asyncio
async def test_an_update_at_another_width_leaves_the_stored_value(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _coded_at(monkeypatch, FixedEmbedder.dim)
    await write_mod.handle_memory_set(
        MemorySetInput(**IDENTITY, memory_key="rv/fact/sky", value="blue", classify=False)
    )
    rows, vectors = len(store.versions), len(store.embeddings)
    _coded_at(monkeypatch, FixedEmbedder.dim * 2)
    with pytest.raises(CodedWidthMismatchError):
        await update_mod.handle_memory_update(
            MemoryUpdateInput(org="o", namespace="n", memory_key="rv/fact/sky", value="green")
        )
    assert (len(store.versions), len(store.embeddings)) == (rows, vectors)
    head = store.head(memory_key="rv/fact/sky", agent="a")
    assert head is not None and "blue" in head["value"]


@pytest.mark.asyncio
async def test_a_set_refuses_only_a_write_that_carries_a_vector(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _coded_at(monkeypatch, FixedEmbedder.dim * 2)
    with pytest.raises(CodedWidthMismatchError):
        await write_mod.handle_memory_set(
            MemorySetInput(**IDENTITY, memory_key="rv/fact/sky", value="blue", classify=False)
        )
    assert store.versions == [] and store.embeddings == []
    await write_mod.handle_memory_set(
        MemorySetInput(
            **IDENTITY, memory_key="rv/key/db", value="hunter2", sensitivity="high", classify=False
        )
    )
    assert [r["memory_key"] for r in store.versions] == ["rv/key/db"]
    assert all(not r.get("embedding") for r in store.embeddings)  # a fence row at most
