"""Working-memory version rows keep the block's `created_at`: append,
replace and delete build on the head they read and carry its first-write
time; only a blind `set` (which reads no head) stamps a new one. CH-free: a
fake client keeps the rows and answers the head read like FINAL."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from server.memory.api.models_working_memory import (
    WorkingMemoryAppendInput,
    WorkingMemoryDeleteInput,
    WorkingMemoryReplaceInput,
    WorkingMemorySetInput,
)
from server.memory.app import working_memory as wm

_ID = {"org": "o", "namespace": "ns", "agent": "ag", "block_key": "plan"}


class _CH:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.working_memory"
        self.rows.extend(dict(r) for r in rows)

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        live = [r for r in self.rows if r["block_key"] == params["key"]]
        if not live:
            return None
        head = max(live, key=lambda r: r["version"])
        return None if head["tombstoned_at"] is not None else dict(head)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> tuple[_CH, dict[str, datetime]]:
    ch = _CH()
    clock = {"now": datetime(2026, 1, 1, 9, 0, 0)}
    counter = iter(range(1, 10_000))

    def _tick() -> datetime:
        clock["now"] += timedelta(minutes=1)
        return clock["now"]

    monkeypatch.setattr(wm, "get_client", lambda: ch)
    monkeypatch.setattr(wm, "_now", _tick)
    monkeypatch.setattr(wm, "next_version", lambda: next(counter))
    return ch, clock


async def test_mutations_carry_the_first_write_time(env) -> None:
    ch, _ = env
    await wm.handle_working_memory_set(WorkingMemorySetInput(**_ID, content="step 1"))
    first = ch.rows[0]["created_at"]
    await wm.handle_working_memory_append(WorkingMemoryAppendInput(**_ID, addition="step 2"))
    await wm.handle_working_memory_replace(
        WorkingMemoryReplaceInput(**_ID, old="step 1", new="step one")
    )
    await wm.handle_working_memory_delete(WorkingMemoryDeleteInput(**_ID))
    assert [r["created_at"] for r in ch.rows] == [first] * 4
    assert len({r["updated_at"] for r in ch.rows}) == 4  # each mutation is its own time
    assert ch.rows[2]["content"] == "step one\nstep 2"
    assert ch.rows[3]["tombstoned_at"] is not None


async def test_append_to_a_missing_block_is_its_first_write(env) -> None:
    ch, _ = env
    await wm.handle_working_memory_append(WorkingMemoryAppendInput(**_ID, addition="only"))
    (row,) = ch.rows
    assert row["content"] == "only" and row["created_at"] == row["updated_at"]


async def test_a_blind_set_reads_no_head_and_starts_a_new_created_at(env) -> None:
    ch, _ = env
    await wm.handle_working_memory_set(WorkingMemorySetInput(**_ID, content="a"))
    await wm.handle_working_memory_set(WorkingMemorySetInput(**_ID, content="b"))
    assert ch.rows[1]["created_at"] > ch.rows[0]["created_at"]
