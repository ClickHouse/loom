"""memory.delete_version against the real loom.memory table on clickhouse-local: a plain
memory.set whose version was drawn before the delete and whose row lands after it keeps
its row, because the tombstone sits right above the version the delete checked."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

import server.memory.app.handlers as hm
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.api.models_crud import MemoryDeleteVersionInput
from server.memory.store import key_write_lock
from server.memory.store.rows import build_first_version_row
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_ID = {"org": "o", "namespace": "n", "agent": "okf-sync", "user_id": "-", "session_id": "-"}
_MID = "00000000-0000-0000-0000-00000000000a"


class _StatefulCH:
    """clickhouse-local that keeps every insert: each query runs after all of them."""

    def __init__(self) -> None:
        self.setup = [ddl("memory")]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        for r in rows:
            cols = ", ".join(r)
            data = json.dumps(r, default=str)
            self.setup.append(
                f"INSERT INTO {table} ({cols}) SELECT {cols} FROM format(JSONEachRow, $${data}$$)"
            )

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = run(self.setup, sql, params)
        return rows[0] if rows else None


def _row(value: str, version: int) -> dict[str, Any]:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    return build_first_version_row(
        {
            **_ID,
            "memory_id": _MID,
            "memory_key": "deploy_window",
            "memory_type": "semantic",
            "value": json.dumps(value),
            "confidence": 1.0,
            "version": version,
            "source_trace": "okf:semantic/deploy_window.md",
        },
        now=now,
    )


@pytest.mark.asyncio
async def test_a_set_landing_during_the_delete_keeps_its_row(monkeypatch) -> None:
    ch = _StatefulCH()

    @asynccontextmanager
    async def _no_lease(*_key: str) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", _no_lease)
    monkeypatch.setattr(hm, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.reads._default_client", lambda: ch)
    head = 1_760_000_000_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    # A plain memory.set draws its version, then embeds; the delete reads the old head and
    # tombstones it before the set's row lands.
    racing = head + 5_000_000
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key="deploy_window", version=head)
    )
    assert out.tombstoned is True
    await ch.insert("loom.memory", [_row("Thursdays", racing)])

    identity = memory_repo.Identity(**_ID)
    got = await memory_repo.get_by_key(
        identity, "deploy_window", strong=True, scope=WHOLE_NAMESPACE, ch=ch
    )
    assert got is not None and got.value == "Thursdays" and got.version == racing
    assert out.version == head + 1


@pytest.mark.asyncio
async def test_the_version_it_checked_is_deleted_when_nothing_races_it(monkeypatch) -> None:
    ch = _StatefulCH()

    @asynccontextmanager
    async def _no_lease(*_key: str) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", _no_lease)
    monkeypatch.setattr(hm, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.reads._default_client", lambda: ch)
    head = 1_760_000_000_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key="deploy_window", version=head)
    )
    assert out.tombstoned is True
    got = await memory_repo.get_by_key(
        memory_repo.Identity(**_ID),
        "deploy_window",
        strong=True,
        scope=WHOLE_NAMESPACE,
        ch=ch,
    )
    assert got is None
