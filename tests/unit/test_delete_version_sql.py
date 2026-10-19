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
async def test_a_conditional_set_in_flight_during_a_delete_is_refused(ch: _StatefulCH) -> None:
    # Its precondition read the row the delete removed; landing would undo the delete. The
    # refusal is memory.set's own precondition answer, so the write is counted as one.
    from fastapi import HTTPException

    w = await _delete_during_the_set(ch, "memory.delete")
    w.input = w.input.model_copy(update={"if_match_sha256": "a" * 64})
    with pytest.raises(HTTPException) as refused:
        await set_commit._set_plain_insert(w, ch)
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "PRECONDITION_FAILED"
    assert await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_delete_of_a_key_a_sweep_once_caught_is_still_a_delete(ch: _StatefulCH) -> None:
    # The sweep's reason rides the row after a restore; the delete's tombstone clears it.
    head = time.time_ns() - 10_000_000_000
    await ch.insert(
        "loom.memory", [{**_row("Tuesdays", head), "expire_reason": "blacklist.sweep:p1"}]
    )
    w = _in_flight_set(head + 5_000_000)
    await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"


@pytest.mark.asyncio
async def test_a_write_drawn_after_the_delete_keeps_winning(ch: _StatefulCH) -> None:
    # A set drawn after the delete lands first; the set in flight across the delete then
    # reaches its fence. The key's newest version is no tombstone, so it is not lifted.
    w = await _delete_during_the_set(ch, "memory.delete")
    later = {**_row("Fridays", time.time_ns()), "memory_id": "00000000-0000-0000-0000-00000000000b"}
    await ch.insert("loom.memory", [later])
    drawn = w.version
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert w.version == drawn and got is not None and got.value == "Fridays"


@pytest.mark.asyncio
async def test_a_blacklist_sweeps_tombstone_keeps_the_write_under_it(ch: _StatefulCH) -> None:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(head + 5_000_000)
    swept = {
        **_row("Tuesdays", time.time_ns()),
        "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
        "expire_reason": "blacklist.sweep:p1",
    }
    await ch.insert("loom.memory", [swept])
    drawn = w.version
    await set_commit._set_plain_insert(w, ch)
    assert w.version == drawn and await _read_back(ch) is None


@pytest.mark.asyncio
async def test_of_two_writes_in_flight_across_a_delete_the_first_fenced_wins(
    ch: _StatefulCH,
) -> None:
    # The deleting replica's clock runs ahead: the first write to reach its fence takes the
    # tombstone's version + 1. The second then finds a live row newest and stays under it,
    # as two writes in flight together resolve in the order they land.
    from server.memory.app.write_fence import lift_above_deletes

    head = time.time_ns() + 60_000_000_000  # the deleting replica runs a minute ahead
    await ch.insert(
        "loom.memory",
        [
            {
                **_row("Tuesdays", head),
                "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
            }
        ],
    )
    first, second = _row("one", head - 2), _row("two", head - 1)
    await lift_above_deletes("o", "n", [first], [], ch=ch)
    await ch.insert("loom.memory", [first])
    await lift_above_deletes("o", "n", [second], [], ch=ch)
    assert first["version"] == head + 1 and second["version"] == head - 1


@pytest.mark.asyncio
async def test_a_fence_that_cannot_read_keeps_the_drawn_version() -> None:
    from server.memory.app.write_fence import lift_above_deletes

    class _Down:
        async def query_all(self, *_a: Any, **_k: Any) -> list[Any]:
            raise RuntimeError("timeout")

    from server.observability import degradations

    degradations.begin()
    row = _row("one", 7)
    assert await lift_above_deletes("o", "n", [row], [], ch=_Down()) == {}
    assert row["version"] == 7
    assert any("write_fence" in d for d in degradations.current())


@pytest.mark.asyncio
async def test_a_bulk_write_in_flight_during_a_delete_lands_with_its_vectors(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.setup[:0] = ["SET enable_quantized_codec = 1", ddl("memory_embeddings")]
    for name in ("index_late_rows", "retire_vectors_for_keys"):
        monkeypatch.setattr(memory_repo, name, _noop)
    monkeypatch.setattr(bulk_set, "fence_high", _noop)
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))

    # Two entries of the key in one batch, drawn before the delete, in this order.
    state = _BulkWriteState()
    for i, value in enumerate(("Wednesdays", "Thursdays")):
        v = head + 1_000 + i
        state.memory_rows.append({**_row(value, v), "memory_type": "semantic"})
        state.embedding_rows.append(
            memory_repo.build_embedding_row(
                **_ID,
                memory_key=_KEY,
                memory_type="semantic",
                content_excerpt=value,
                version=v,
                embedding=[0.0] * 3072,
                embedding_dim=3072,
                memory_key_embedding=[0.0] * 3072,
            )
        )
        state.results.append(_BulkSetResult(memory_key=_KEY, version=v))
    await bulk_set._bulk_insert(ch, state)

    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"
    assert [r.version for r in state.results] == sorted(r.version for r in state.results)
    assert state.results[1].version == got.version
    vectors = await ch.query_all(
        "SELECT content_excerpt, version FROM loom.memory_embeddings FINAL WHERE tombstoned_at IS NULL"
    )
    assert [(v["content_excerpt"], int(v["version"])) for v in vectors] == [
        ("Thursdays", got.version)
    ]


@pytest.mark.asyncio
async def test_an_embedded_set_in_flight_during_a_delete_lands_with_its_vector(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # memory.set's embedded path builds its vector from the lifted version.
    from types import SimpleNamespace

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.setup[:0] = ["SET enable_quantized_codec = 1", ddl("memory_embeddings")]
    monkeypatch.setattr(memory_repo, "index_late_rows", _noop)
    monkeypatch.setattr(set_commit._cd, "schedule_settle", lambda *_a, **_k: None)
    w = await _delete_during_the_set(ch, "memory.delete")
    w.input = w.input.model_copy(update={"memory_type": "semantic"})
    w.memory_row["memory_type"] = "semantic"
    w.embedder = SimpleNamespace(dim=3072, model_name="m")
    w.excerpt, w.embedding = "Thursdays", [0.0] * 3072
    w.key_embedding, w.key_text = [0.0] * 3072, "deploy window"
    verdict = SimpleNamespace(verdict="", peers=[], refined_peers=[])
    await set_commit._set_dual_write(w, ch, None, verdict)

    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version
    vectors = await ch.query_all(
        "SELECT version FROM loom.memory_embeddings FINAL WHERE tombstoned_at IS NULL"
    )
    assert [int(v["version"]) for v in vectors] == [w.version]
