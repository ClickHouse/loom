"""Category links run through `server.memory.store.categories`: a namespace's
registered names resolve in one read, and a key is linked or its link rescinded with
one row each. memory.assign_category links through them and answers a name the
namespace has not registered with a 404."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_graph import MemoryAssignCategoryInput
from server.memory.app.ops import handle_memory_assign_category
from server.memory.store import categories


class _CategoriesCH:
    """loom.categories as a name → id `registry`; inserts recorded."""

    def __init__(self, registry: dict[str, str]) -> None:
        self.registry = registry
        self.reads: list[dict[str, Any]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        assert "FROM loom.categories FINAL" in sql and "tombstoned_at IS NULL" in sql
        params = params or {}
        self.reads.append(params)
        return [
            {"name": n, "category_id": self.registry[n]}
            for n in params["names"]
            if n in self.registry
        ]

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.inserts.append((table, rows))


@pytest.mark.asyncio
async def test_registered_names_resolve_in_one_read_and_others_are_absent() -> None:
    ch = _CategoriesCH({"pets": "cat-1", "work": "cat-2"})
    ids = await categories.registered_ids("o", "n", ["work", "pets", "nope", "pets"], ch=ch)
    assert ids == {"pets": "cat-1", "work": "cat-2"}
    assert [r["names"] for r in ch.reads] == [["nope", "pets", "work"]]
    assert (ch.reads[0]["org"], ch.reads[0]["ns"]) == ("o", "n")


@pytest.mark.asyncio
async def test_no_names_send_no_statement() -> None:
    ch = _CategoriesCH({"pets": "cat-1"})
    assert await categories.registered_ids("o", "n", [], ch=ch) == {}
    await categories.link("o", "n", [], ch=ch)
    assert ch.reads == [] and ch.inserts == []


@pytest.mark.asyncio
async def test_a_link_is_one_row_and_a_rescinded_one_carries_its_tombstone() -> None:
    ch = _CategoriesCH({})
    await categories.link("o", "n", [("k1", "cat-1"), ("k2", "cat-1")], ch=ch)
    await categories.link("o", "n", [("k1", "cat-1")], tombstone=True, ch=ch)
    tables = {table for table, _ in ch.inserts}
    rows = [row for _, batch in ch.inserts for row in batch]
    assert tables == {"loom.memory_categories"}
    assert [(r["memory_key"], r["category_id"], r["tombstoned_at"] is None) for r in rows] == [
        ("k1", "cat-1", True),
        ("k2", "cat-1", True),
        ("k1", "cat-1", False),
    ]
    assert all((r["org"], r["namespace"]) == ("o", "n") and r["created_at"] for r in rows)


class _LinksCH:
    """loom.memory_categories answering `rows`, the newest version of each link."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.reads: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.reads.append((sql, params))
        return [r for r in self.rows if r["memory_key"] in params["keys"]]


@pytest.mark.asyncio
async def test_link_states_reads_live_and_rescinded_links_of_the_keys() -> None:
    ch = _LinksCH(
        [
            {"memory_key": "k1", "category_id": "cat-1", "live": 1},
            {"memory_key": "k2", "category_id": "cat-1", "live": 0},
            {"memory_key": "k3", "category_id": "cat-1", "live": 1},
        ]
    )
    states = await categories.link_states("o", "n", ["k2", "k1", "k1"], ch=ch)
    assert states == {("k1", "cat-1"): True, ("k2", "cat-1"): False}
    ((sql, params),) = ch.reads
    assert "FROM loom.memory_categories FINAL" in sql and "select_sequential_consistency" in sql
    assert (params["org"], params["ns"], params["keys"]) == ("o", "n", ["k1", "k2"])


@pytest.mark.asyncio
async def test_assign_category_links_rescinds_and_refuses_an_unregistered_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CategoriesCH({"pets": "cat-1"})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    ask = {"org": "o", "namespace": "n", "memory_key": "k", "category_name": "pets"}
    out = await handle_memory_assign_category(MemoryAssignCategoryInput(**ask))
    assert (out.assigned, out.category_id) == (True, "cat-1")
    rescinded = await handle_memory_assign_category(
        MemoryAssignCategoryInput(**ask, tombstone=True)
    )
    assert (rescinded.assigned, rescinded.category_id) == (False, "cat-1")
    rows = [row for _, batch in ch.inserts for row in batch]
    assert [(r["memory_key"], r["tombstoned_at"] is None) for r in rows] == [
        ("k", True),
        ("k", False),
    ]
    with pytest.raises(HTTPException) as caught:
        await handle_memory_assign_category(
            MemoryAssignCategoryInput(**{**ask, "category_name": "nope"})
        )
    assert caught.value.status_code == 404
    assert isinstance(caught.value.detail, dict)
    assert caught.value.detail["code"] == "CATEGORY_NOT_FOUND"
    assert len(ch.inserts) == 2
