"""memory.provenance offers a live transcript link, and its title, only for a conversation
that is still retained: a deleted or erased thread's tombstoned rows count for nothing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_lifecycle import MemoryProvenanceInput
from server.memory.app import evidence_reads as evidence_reads


class _CH:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> Any:
        self.sql.append(sql)
        if "FROM loom.conversations" in sql:
            return {"n": 0 if "tombstoned_at IS NULL" in sql else 3}
        if "FROM loom.conversation_threads" in sql:
            return {"title": "the erased chat"}
        return None

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[Any]:
        return []


async def test_an_erased_thread_is_not_live_and_withholds_its_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH()

    async def _row(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(source_trace="conversation:t-1")

    monkeypatch.setattr("server.memory.store.get_by_key", _row)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await evidence_reads.handle_memory_provenance(
        MemoryProvenanceInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    assert out.found
    assert out.thread_exists is False and out.thread_title == ""
