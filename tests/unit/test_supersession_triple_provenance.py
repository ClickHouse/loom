"""A successor triple written when a row supersedes another carries its
predecessor's provenance, through the same row builder the canonical triple
writer uses."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import entity_projections as proj
from server.memory.app.projections import entity_triple_row

A, B = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "FROM loom.entity_triple_rows" in sql:
            return []  # rows written before rows were linked: matched by their text
        if "FROM loom.memory FINAL" in sql:
            return [
                {
                    "id": A,
                    "memory_key": "old",
                    "value": "The chairperson of Fatah is Mahmoud Abbas.",
                    "valid_at": None,
                    "status": "superseded",
                    "superseded_by": B,
                    "tombstoned_at": None,
                },
                {
                    "id": B,
                    "memory_key": "new",
                    "value": "The chairperson of Fatah is Moshe Kahlon.",
                    "valid_at": None,
                    "status": "active",
                    "superseded_by": "",
                    "tombstoned_at": None,
                    "holds_facts": 1,
                },
            ]
        if "triples" in (params or {}):
            return []  # no successor is open yet
        assert "provenance" in sql, "the open-triple read must fetch provenance"
        return [
            {
                "subject": "fatah",
                "relation": "chairperson_of",
                "object": "mahmoud abba",
                "confidence": 0.9,
                "provenance": "extracted",
            }
        ]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))

    async def command(self, *a: Any, **k: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_the_successor_triple_carries_the_predecessor_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_close(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(proj, "close_triples", _no_close)
    ch = _CH()
    closed, inserted, _ = await proj.follow_row_supersessions(
        SimpleNamespace(org="o", namespace="n", agent="a"), [(A, B)], ch=ch
    )
    assert (closed, inserted) == (1, 1)
    [row] = next(rows for table, rows in ch.inserts if table == "loom.entity_triples")
    canonical = entity_triple_row(
        "o",
        "n",
        "fatah",
        "chairperson_of",
        "moshe kahlon",
        provenance="extracted",
        confidence=0.9,
        adapter_name="row-supersession",
        last_seen=row["last_seen"],
        seen_at=row["seen_at"],
    )
    assert isinstance(row["last_seen"], datetime)
    assert row["seen_at"].replace(microsecond=0) == row["last_seen"]
    assert row == canonical
