"""Write-side event dating: triples inherit the fact's temporal_anchor,
and the schema-mode (point-extraction) path stamps valid_at from the
conversation's observation_date.

CH-free: stub clients capture inserts/queries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult


class _StubCH:
    """Captures inserts; query_all returns nothing (repair leg no-op)."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        return []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows, settings or {}))


def _bulk_input(**entry_kwargs) -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="dev",
        namespace="ns",
        agent="a",
        user_id="alice",
        session_id="s1",
        entries=[
            _BulkSetEntry(
                memory_key="k1",
                value="v",
                relations=[("user", "visited", "golden gate park")],
                **entry_kwargs,
            )
        ],
    )


async def _written_triples(inp: MemoryBulkSetInput) -> list[dict[str, Any]]:
    from server.memory.app.projections import write_entity_triples

    ch = _StubCH()
    await write_entity_triples(
        inp, [_BulkSetResult(memory_key="k1", version=0)], ch=ch, entity_map={}
    )
    by_table = {t: rows for t, rows, _ in ch.inserts}
    return by_table.get("loom.entity_triples", [])


@pytest.mark.asyncio
async def test_triple_valid_from_falls_back_to_temporal_anchor() -> None:
    triples = await _written_triples(_bulk_input(temporal_anchor="2023-05-07"))
    assert len(triples) == 1
    assert triples[0]["valid_from"] == datetime(2023, 5, 7)


@pytest.mark.asyncio
async def test_triple_valid_from_prefers_valid_at_over_anchor() -> None:
    triples = await _written_triples(
        _bulk_input(valid_at=datetime(2023, 1, 1), temporal_anchor="2023-05-07")
    )
    assert triples[0]["valid_from"] == datetime(2023, 1, 1)


@pytest.mark.asyncio
async def test_triple_valid_from_null_when_undated() -> None:
    triples = await _written_triples(_bulk_input())
    assert triples[0]["valid_from"] is None


# ----------------------------------------------------------------------
# Schema-mode (point-extraction) valid_at default
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_mode_entry_defaults_valid_at_from_observation_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx
    from server.llm.client import LLMResult
    from server.memory.api.models_bulk import MemorySetFromMessagesInput
    from server.memory.app.extract import ExtractedRecord

    captured: dict[str, Any] = {}

    async def _fake_bulk(bulk_input):
        captured["input"] = bulk_input

        class _Out:
            written: list = []

        return _Out()

    async def _fake_evidence(input, msgs, **kw):
        return []

    async def _fake_link(*args, **kwargs):
        return 0

    async def _fake_inline(*args, **kwargs):
        return None

    async def _fake_typed(
        msgs, schema, *, max_tokens=1024, observation_date=None, operator_policy=""
    ):
        return (
            ExtractedRecord(record={"name": "x"}),
            LLMResult(text="", tokens_in=0, tokens_out=0, provider="p", model="m"),
        )

    monkeypatch.setattr(mx, "handle_memory_bulk_set", _fake_bulk)
    monkeypatch.setattr(mx, "_write_source_evidence", _fake_evidence)
    monkeypatch.setattr(mx, "_link_written_memories_to_evidence", _fake_link)
    monkeypatch.setattr(mx, "_write_inline_record_projection", _fake_inline)
    monkeypatch.setattr(
        "server.memory.app.extract.extract_typed_record_iterative",
        _fake_typed,
    )

    await mx.handle_memory_set_from_messages(
        MemorySetFromMessagesInput(
            org="dev",
            namespace="ns",
            agent="a",
            messages=[{"role": "user", "content": "name is x"}],
            memory_key="rec1",
            record_schema={"name": "string"},
            observation_date="2023-05-07",
        )
    )

    entry = captured["input"].entries[0]
    assert entry.valid_at == datetime(2023, 5, 7)
