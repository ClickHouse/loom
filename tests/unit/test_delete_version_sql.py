"""A delete that commits while a memory.set is in flight, against the real loom.memory table
on clickhouse-local. The set drew its version before the delete (it embeds in between), so
the tombstone's version is the higher one; the set's insert takes a new version above the
tombstone, and its row is the one read back. The derived tables (vectors, chunks) are
absent: their best-effort retires log, and the row tombstone stands."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

import server.memory.app.handlers as hm
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.api.models_crud import (
    MemoryDeleteInput,
    MemoryDeleteVersionInput,
    MemorySetInput,
)
from server.memory.app import set_commit
from server.memory.store import key_write_lock
from server.memory.store.rows import build_first_version_row
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_ID = {"org": "o", "namespace": "n", "agent": "okf-sync", "user_id": "-", "session_id": "-"}
_MID = "00000000-0000-0000-0000-00000000000a"
_KEY = "deploy_window"


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
            "memory_key": _KEY,
            "memory_type": "procedural",  # not embedded: memory.set's plain insert
            "value": json.dumps(value),
            "value_json": "{}",
            "content_sha256": "",
            "actor": "process:okf-sync",
            "source_class": "imported",
            "confidence": 1.0,
            "version": version,
        },
        now=now,
    )


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _StatefulCH:
    ch = _StatefulCH()

    @asynccontextmanager
    async def _no_lease(*_key: str) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", _no_lease)
    monkeypatch.setattr(hm, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.reads._default_client", lambda: ch)
    return ch


def _in_flight_set(version: int) -> set_commit._SetWrite:
    """memory.set's state after its embed: the row built at the version drawn at the start."""
    w = set_commit._SetWrite(
        input=MemorySetInput(**_ID, memory_key=_KEY, value="Thursdays", memory_type="procedural"),
        version=version,
        retyped_from="",
        existing=None,
        expires_at=None,
    )
    w.memory_row = _row("Thursdays", version)
    w.memory_id = _MID
    return w


async def _read_back(ch: _StatefulCH) -> Any:
    return await memory_repo.get_by_key(
        memory_repo.Identity(**_ID), _KEY, strong=True, scope=WHOLE_NAMESPACE, ch=ch
    )


async def _delete_during_the_set(ch: _StatefulCH, op: str) -> set_commit._SetWrite:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(head + 5_000_000)  # drawn after the head, before the delete
    if op == "memory.delete":
        out: Any = await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
    else:
        out = await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    assert out.tombstoned is True and out.version > w.version
    return w


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["memory.delete_version", "memory.delete"])
async def test_a_set_in_flight_during_a_delete_keeps_its_row(ch: _StatefulCH, op: str) -> None:
    w = await _delete_during_the_set(ch, op)
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version


@pytest.mark.asyncio
async def test_without_the_lift_the_tombstone_hides_the_set(ch: _StatefulCH) -> None:
    # The race the lift closes: the same insert at the version drawn before the delete.
    w = await _delete_during_the_set(ch, "memory.delete_version")
    await ch.insert("loom.memory", [w.memory_row])
    assert await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_tombstone_older_than_the_write_leaves_its_version(ch: _StatefulCH) -> None:
    # The key was deleted before this write began: the version drawn at its start stands.
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    w = _in_flight_set(time.time_ns())
    drawn = w.version
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert got is not None and got.version == drawn


@pytest.mark.asyncio
async def test_a_bulk_write_in_flight_during_a_delete_is_lifted_with_its_vectors(
    ch: _StatefulCH,
) -> None:
    from server.memory.app.write_fence import lift_above_tombstones

    w = await _delete_during_the_set(ch, "memory.delete_version")
    drawn = w.version
    other = {**_row("other key", drawn + 1), "memory_key": "unrelated"}
    vector = {**_ID, "memory_key": _KEY, "version": drawn}
    lifted = await lift_above_tombstones("o", "n", [w.memory_row, other], [vector], ch=ch)
    assert list(lifted) == [drawn] and w.memory_row["version"] == lifted[drawn]
    assert vector["version"] == lifted[drawn] and other["version"] == drawn + 1
