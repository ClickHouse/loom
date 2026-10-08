"""memory.set: a byte-identical rewrite of the live value is a reinforcement:
the version row carries base + 1 and a fresh mentioned_at."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import server.memory.app.handlers as mem_handlers
from server.core.hashing import sha256_value
from server.memory.api.models_crud import MemorySetInput
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.app import write as write_mod
from server.memory.store import MemoryRow

_NOW = datetime(2026, 9, 12, 12, 0, 0)


def _row(value: str, reinforcement_count: int = 3) -> MemoryRow:
    return MemoryRow(
        memory_id="11111111-1111-1111-1111-111111111111",
        org="test",
        namespace="personal",
        agent="cursor",
        user_id="-",
        session_id="-",
        memory_key="deploy_window",
        memory_type="semantic",
        value=value,
        confidence=0.9,
        version=100,
        pinned=False,
        created_at=_NOW - timedelta(days=3),
        expires_at=None,
        content_sha256=sha256_value(value),
        reinforcement_count=reinforcement_count,
    )


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None


def _wire(monkeypatch: pytest.MonkeyPatch, existing: MemoryRow) -> _CH:
    ch = _CH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    import server.memory.app.blacklist_guard as _bl
    import server.memory.app.conflicts as _cf

    monkeypatch.setattr(_bl, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(_cf, "get_client", lambda: ch, raising=False)

    async def _existing(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _existing)
    monkeypatch.setattr("server.memory.store.get_by_id", _existing)
    return ch


def _memory_row(ch: _CH) -> dict[str, Any]:
    rows = [r for t, rs in ch.inserts if t == "loom.memory" for r in rs]
    assert rows, "memory.set wrote a loom.memory row"
    return rows[0]


@pytest.mark.asyncio
async def test_identical_rewrite_carries_base_plus_one_and_a_fresh_mention(monkeypatch) -> None:
    ch = _wire(monkeypatch, _row("Tue and Thu only", reinforcement_count=3))
    await write_mod.handle_memory_set(
        MemorySetInput(
            org="test",
            namespace="personal",
            agent="cursor",
            memory_key="deploy_window",
            value="Tue and Thu only",
            classify=False,
        )
    )
    row = _memory_row(ch)
    assert row["reinforcement_count"] == 4
    assert row["mentioned_at"] is not None
    assert datetime.now(UTC).replace(tzinfo=None) - row["mentioned_at"] < timedelta(minutes=5)


@pytest.mark.asyncio
async def test_a_changed_value_starts_over_at_one(monkeypatch) -> None:
    ch = _wire(monkeypatch, _row("Tue and Thu only", reinforcement_count=3))
    await write_mod.handle_memory_set(
        MemorySetInput(
            org="test",
            namespace="personal",
            agent="cursor",
            memory_key="deploy_window",
            value="Fridays too",
            classify=False,
        )
    )
    row = _memory_row(ch)
    assert row["reinforcement_count"] == 1 and row["mentioned_at"] is None
