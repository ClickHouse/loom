"""A row's `entities` are stored in the canonical form search reads them in: a search's
entity filter is canonicalised (`lookup_canonical_entities`), so a plural or a leading
article on the write side would otherwise never meet it."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.domain.graph_quality import canonical_entities
from server.memory.store import inserts


class _CH:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        self.rows[table] = rows


def test_canonical_entities_fold_surface_forms_once_in_order() -> None:
    assert canonical_entities(["Billing Services", "billing service", "The Ledger", ""]) == [
        "billing service",
        "ledger",
    ]


@pytest.mark.asyncio
async def test_both_insert_funnels_store_canonical_entities(monkeypatch) -> None:
    monkeypatch.setattr(inserts, "check_memory_rows", lambda rows: None)
    ch = _CH()
    await inserts.insert_memory_versions([{"entities": ["Billing Services"]}], ch=ch)
    await inserts.insert_embedding_rows([{"entities": ["Billing Services"], "tags": []}], ch=ch)
    assert ch.rows["loom.memory"][0]["entities"] == ["billing service"]
    assert ch.rows["loom.memory_embeddings"][0]["entities"] == ["billing service"]
