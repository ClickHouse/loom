"""Stand-ins memory.bulk_set's write path can run against with no ClickHouse:
a recording client, a batch embedder, and the projections and lookups it calls
patched out."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app import session_summary as _session_summary_mod
from tests.unit.memory_rows import projection_row


def no_delete_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """No delete races these writes: the fence before the insert finds no later version."""

    async def _none(*_a: Any, **_k: Any) -> dict[Any, int]:
        return {}

    monkeypatch.setattr("server.memory.store.versions_above", _none)


class StubCH:
    def __init__(self, lookup_rows: list[dict[str, Any]] | None = None) -> None:
        # The live rows the batched lookup returns, as the full projection has them.
        self.lookup_rows = [
            projection_row(
                org="test", namespace="personal", agent="cursor", user_id="u", session_id="s", **r
            )
            for r in (lookup_rows or [])
        ]
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "FROM loom.memory" in sql:
            return self.lookup_rows
        return []

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.insert_calls.append((table, rows, settings or {}))

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows, _ in self.insert_calls for r in rows if t == table]


class BatchEmbedder:
    """Distinct vector per input text, so batch alignment is observable."""

    model_name = "fake-test"
    dim = 3

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[float(i + 1)] * 3 for i in range(len(texts))]


def wire_stubs(
    monkeypatch: pytest.MonkeyPatch,
    ch: StubCH,
    embedder: BatchEmbedder,
    captured_embedding_rows: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: embedder)
    monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: embedder)

    async def _find_match(**_: Any) -> None:
        return None

    async def _note_hit(*_: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _find_match)
    monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _note_hit)

    async def _resolve_entities_for_write(*_: Any, **__: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(
        "server.memory.app.entity_resolution.resolve_entities_for_write",
        _resolve_entities_for_write,
    )

    async def _insert_embedding_rows(rows: list[dict[str, Any]], *, ch: Any) -> None:
        captured_embedding_rows.extend(rows)

    monkeypatch.setattr(
        "server.memory.store.insert_embedding_rows",
        _insert_embedding_rows,
    )

    async def _noop_projection(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(_bulk_set_mod, "write_entity_vectors", _noop_projection)
    monkeypatch.setattr(_bulk_set_mod, "write_entity_edges", _noop_projection)
    monkeypatch.setattr(_bulk_set_mod, "write_entity_triples", _noop_projection)
    monkeypatch.setattr(_bulk_set_mod, "_write_derived_facts", _noop_projection)
