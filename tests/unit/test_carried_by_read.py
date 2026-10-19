"""The value history `_carried_by` reads decides which triples a live row keeps
(follow, the close of dropped facts, Undo): it must see a version written a moment
ago on another replica."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import graph_holders


class _StubCH:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.sql.append(sql)
        return []


@pytest.mark.asyncio
async def test_the_version_read_is_sequentially_consistent(monkeypatch) -> None:
    async def _no_cardinality(*_: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(
        "server.identity.app.namespace_ontology.load_relation_cardinality", _no_cardinality
    )
    ch = _StubCH()
    triple = ("user", "live_in", "paris")
    await graph_holders._carried_by(
        "o",
        "n",
        [{"id": "m1", "content_sha256": "new", "value": '"User lives in Rome."'}],
        {},
        {"m1": {triple: {"old"}}},
        ch=ch,
    )
    (sql,) = [s for s in ch.sql if "loom.memory_versions" in s]
    assert "select_sequential_consistency = 1" in sql
