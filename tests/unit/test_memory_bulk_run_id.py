from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app import session_summary as _session_summary_mod
from server.memory.app.extract import handler as mem_extract


class _StubCH:
    def __init__(self) -> None:
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return []

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.insert_calls.append((table, rows, settings or {}))


class _StubEmbedder:
    model_name = "fake-test"
    dim = 3

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.mark.asyncio
async def test_bulk_set_preserves_run_id_on_memory_and_embedding_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _StubCH()
    captured_embedding_rows: list[dict[str, Any]] = []

    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: _StubEmbedder())
    monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: _StubEmbedder())

    async def _find_match(**_: Any) -> None:
        return None

    async def _note_hit(*_: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _find_match)
    monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _note_hit)

    async def _resolve_entities_for_write(*_: Any, **__: Any) -> dict[str, str]:
        return {}

    async def _insert_embedding_rows(
        rows: list[dict[str, Any]],
        *,
        ch: Any,
    ) -> None:
        captured_embedding_rows.extend(rows)

    async def _noop_projection(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(
        "server.memory.app.entity_resolution.resolve_entities_for_write",
        _resolve_entities_for_write,
    )
    monkeypatch.setattr(
        "server.memory.store.insert_embedding_rows",
        _insert_embedding_rows,
    )
    monkeypatch.setattr(_bulk_set_mod, "write_entity_vectors", _noop_projection)
    monkeypatch.setattr(_bulk_set_mod, "write_entity_edges", _noop_projection)
    monkeypatch.setattr(_bulk_set_mod, "write_entity_triples", _noop_projection)

    out = await mem_extract.handle_memory_bulk_set(
        MemoryBulkSetInput(
            org="test",
            namespace="personal",
            agent="cursor",
            user_id="u",
            session_id="s",
            run_id="run-42",
            entries=[
                _BulkSetEntry(
                    memory_key="deploy_window",
                    memory_type="semantic",
                    value="Deploys happen Tuesday and Thursday only.",
                )
            ],
        )
    )

    assert [r.memory_key for r in out.written] == ["deploy_window"]
    memory_rows = [rows for table, rows, _ in ch.insert_calls if table == "loom.memory"]
    assert memory_rows
    assert memory_rows[0][0]["run_id"] == "run-42"
    assert captured_embedding_rows
    assert captured_embedding_rows[0]["run_id"] == "run-42"
