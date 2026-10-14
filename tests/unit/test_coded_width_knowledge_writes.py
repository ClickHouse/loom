"""A knowledge write whose vectors the coded `chunks` column cannot take is
refused before it reads or writes anything: no document, chunk or supersession
lands half-done."""

from __future__ import annotations

from typing import Any

import pytest

from server.core import ch as ch_module
from server.core.errors import CodedWidthMismatchError
from server.core.vectors import codes_state
from server.knowledge.api.models_knowledge import (
    KnowledgeAddInput,
    KnowledgeBulkAddInput,
    KnowledgeUpsertInput,
)
from server.knowledge.app import writes
from tests.unit.memory_store import FixedEmbedder


class _Untouched:
    """A ClickHouse client the refused write must never use."""

    def __init__(self) -> None:
        self.used: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.used.append(name)
        raise AssertionError(f"the refused write called ch.{name}")


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _Untouched:
    client = _Untouched()
    monkeypatch.setattr(ch_module, "get_client", lambda: client)
    monkeypatch.setattr(writes, "get_embedder", lambda: FixedEmbedder())
    monkeypatch.setattr(codes_state._state, "widths", {"loom.chunks.embedding": FixedEmbedder.dim * 2})
    return client


DOC = {"org": "o", "namespace": "n", "source_uri": "doc://a", "content": "the sky is blue"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "write",
    [
        lambda: writes.handle_knowledge_add(KnowledgeAddInput(**DOC)),
        lambda: writes.handle_knowledge_upsert(KnowledgeUpsertInput(**DOC)),
        lambda: writes.handle_knowledge_bulk_add(
            KnowledgeBulkAddInput(
                org="o",
                namespace="n",
                entries=[{"source_uri": "doc://a", "content": "the sky is blue"}],
            )
        ),
    ],
    ids=["add", "upsert", "bulk_add"],
)
async def test_a_knowledge_write_at_another_width_touches_nothing(ch: _Untouched, write) -> None:
    with pytest.raises(CodedWidthMismatchError):
        await write()
    assert ch.used == []
    await codes_state.refuse_width("chunks", FixedEmbedder.dim * 2)  # the stored width passes
    await codes_state.refuse_width("memory_embeddings", 7)  # not coded: nothing to refuse
