"""The queue locates its rows, then reads them.

Selecting `value` under FINAL with a filter outside the sort key
(`status = 'conflicted'`) would make ClickHouse read every value in the
namespace to keep the few conflicted rows. The listing locates the page on
the key columns under FINAL and fetches the values by (memory_id, version)
pairs without FINAL, the bulk-fetch shape. The wire shape does not change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.store import conflicts as conflicts_repo


class _CH:
    def __init__(self, located: list[dict[str, Any]], values: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers = [located, values]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.calls.append((sql, dict(params or {})))
        return self._answers.pop(0) if self._answers else []


_T = datetime(2026, 9, 19, 10, 0, 0)


@pytest.mark.asyncio
async def test_the_locate_statement_reads_no_value_and_the_fetch_reads_no_final() -> None:
    located = [
        {
            "memory_id": "a",
            "memory_key": "k1",
            "memory_type": "fact",
            "version": 20,
            "pinned": False,
            "created_at": _T,
            "conflict_with": ["b"],
            "sensitivity": "",
        },
        {
            "memory_id": "c",
            "memory_key": "k2",
            "memory_type": "fact",
            "version": 10,
            "pinned": True,
            "created_at": _T,
            "conflict_with": [],
            "sensitivity": "",
        },
    ]
    values = [
        {"memory_id": "c", "version": 10, "value": '"two"'},
        {"memory_id": "a", "version": 20, "value": '"one"'},
    ]
    ch = _CH(located, values)
    rows = await conflicts_repo.list_conflicted(
        "o", "n", before_version=99, limit=50, ch=ch, scope=WHOLE_NAMESPACE
    )
    assert len(ch.calls) == 2
    locate_sql, locate_params = ch.calls[0]
    fetch_sql, fetch_params = ch.calls[1]
    assert "FROM loom.memory FINAL" in locate_sql
    assert "status = 'conflicted'" in locate_sql and "version < {bv:UInt64}" in locate_sql
    select = locate_sql[locate_sql.index("SELECT") : locate_sql.index("FROM")]
    assert " value" not in select and "value," not in select
    assert locate_params["limit"] == 50 and locate_params["bv"] == 99
    assert "FINAL" not in fetch_sql
    assert "(memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}" in fetch_sql
    assert "LIMIT 1 BY memory_id, version" in fetch_sql
    assert fetch_params["pairs"] == [("a", 20), ("c", 10)]
    # the page keeps its order and every column the wire shape needs
    assert [r["memory_key"] for r in rows] == ["k1", "k2"]
    assert rows[0]["value"] == '"one"' and rows[1]["value"] == '"two"'
    assert rows[0]["conflict_with"] == ["b"] and rows[1]["pinned"] is True


@pytest.mark.asyncio
async def test_an_empty_queue_is_one_statement() -> None:
    ch = _CH([], [])
    assert await conflicts_repo.list_conflicted("o", "n", ch=ch, scope=WHOLE_NAMESPACE) == []
    assert len(ch.calls) == 1


@pytest.mark.asyncio
async def test_a_row_whose_value_vanished_between_the_statements_is_null_not_lost() -> None:
    located = [
        {
            "memory_id": "a",
            "memory_key": "k1",
            "memory_type": "fact",
            "version": 20,
            "pinned": False,
            "created_at": _T,
            "conflict_with": [],
            "sensitivity": "",
        }
    ]
    ch = _CH(located, [])
    (row,) = await conflicts_repo.list_conflicted("o", "n", ch=ch, scope=WHOLE_NAMESPACE)
    assert row["memory_key"] == "k1" and row["value"] is None


@pytest.mark.asyncio
async def test_two_identities_sharing_a_key_and_version_each_get_their_own_value() -> None:
    """(memory_key, version) is not unique in a namespace: the entity pass
    stamps one version on every row of its batch, the same key under two
    agents included. The fetch keys on memory_id, so neither row borrows the
    other's value."""
    located = [
        {
            "memory_id": mid,
            "memory_key": "k",
            "memory_type": "fact",
            "version": 7,
            "pinned": False,
            "created_at": _T,
            "conflict_with": [],
            "sensitivity": "",
        }
        for mid in ("m-a", "m-b")
    ]
    values = [
        {"memory_id": "m-b", "version": 7, "value": '"b"'},
        {"memory_id": "m-a", "version": 7, "value": '"a"'},
    ]
    ch = _CH(located, values)
    rows = await conflicts_repo.list_conflicted("o", "n", ch=ch, scope=WHOLE_NAMESPACE)
    assert ch.calls[1][1]["pairs"] == [("m-a", 7), ("m-b", 7)]
    assert [(r["memory_id"], r["value"]) for r in rows] == [("m-a", '"a"'), ("m-b", '"b"')]
