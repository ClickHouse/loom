"""Erasing a user or namespace rewrites the code-link worker's cursors that name them."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.erasure.store import steps_memory as steps

_QUEUE = [17, 3, "o", "ns", "agent", "alice", "s1", "k"]
_BACKFILL = {"after": ["o", "ns", "agent", "alice", "s1", "k"]}
_SWEEP = {"after": ["o", "code", "a.py", "a::f", "ns", "1f3d0b0e-0000-0000-0000-000000000001"]}


def test_a_cursor_naming_an_erased_memory_loses_the_identity() -> None:
    assert json.loads(steps._scrubbed("queue", json.dumps(_QUEUE), "o", set(), "alice")) == [
        17,
        3,
        *[""] * 6,
    ]
    backfill = steps._scrubbed("backfill", json.dumps(_BACKFILL), "o", {"ns"}, "")
    assert json.loads(backfill or "") == {"after": ["o", "ns", "", "", "", ""]}
    sweep = steps._scrubbed("sweep", json.dumps(_SWEEP), "o", set(), "alice")
    assert json.loads(sweep or "")["after"][-1] == steps._NO_MEMORY
    # another org, user or namespace: left as it was
    assert steps._scrubbed("queue", json.dumps(_QUEUE), "o", set(), "bob") is None
    assert steps._scrubbed("backfill", json.dumps(_BACKFILL), "p", {"ns"}, "") is None
    assert steps._scrubbed("recheck/o/code", '{"head": "c1"}', "o", {"ns"}, "") is None


class _CH:
    def __init__(self, queue: list[Any] = _QUEUE) -> None:
        self.queue = queue
        self.sql: list[tuple[str, dict[str, Any]]] = []
        self.inserted: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append((sql, dict(params or {})))
        if "code_index_state" in sql:
            return [{"state_key": "queue", "state_value": json.dumps(self.queue), "version": 9}]
        return []

    async def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append((sql, dict(params or {})))

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.extend(rows)


@pytest.mark.asyncio
async def test_the_scrub_writes_the_cursor_anew_and_deletes_what_it_was() -> None:
    ch = _CH()
    await steps._scrub_link_cursors("o", set(), "alice", ch)  # type: ignore[arg-type]
    (row,) = ch.inserted
    assert (row["state_key"], row["version"], json.loads(row["state_value"])[2:]) == (
        "queue",
        10,
        [""] * 6,
    )
    delete, params = ch.sql[-1]
    assert delete.startswith("DELETE FROM loom.code_index_state") and params["v"] == 10


@pytest.mark.asyncio
async def test_the_scrub_deletes_older_versions_when_the_current_one_names_someone_else() -> None:
    # FINAL reads bob's position; an older version not merged away yet may hold alice's
    ch = _CH([*_QUEUE[:5], "bob", *_QUEUE[6:]])
    await steps._scrub_link_cursors("o", set(), "alice", ch)  # type: ignore[arg-type]
    assert ch.inserted == []
    delete, params = ch.sql[-1]
    assert delete.startswith("DELETE FROM loom.code_index_state")
    assert (params["key"], params["v"]) == ("queue", 9)
    read, params = ch.sql[0]
    assert params["keys"] == ["queue", "backfill", "sweep"]
