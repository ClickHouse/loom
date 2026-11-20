"""Plain point writes leave the same hash-only receipt as searchable writes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.core.hashing import sha256_value
from server.memory.api.models_crud import MemorySetInput
from server.memory.app import set_commit
from server.memory.app.high_fence import CommitAnswer
from server.memory.store.pg_memory import AUDIT_ROW, MEMORY_ROW
from server.observability import audit

_SECRET = "private value that must never enter the audit receipt"


@pytest.fixture
def plain(monkeypatch: pytest.MonkeyPatch) -> Any:
    writes: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []

    async def insert(rows: list[dict[str, Any]], **kwargs: Any) -> None:
        if on_send := kwargs.get("on_send"):
            on_send()
        writes.extend(rows)
        if on_inserted := kwargs.get("on_inserted"):
            on_inserted()

    async def emit(**kwargs: Any) -> str:
        assert len(writes) == 1
        receipts.append(kwargs)
        return "receipt"

    monkeypatch.setattr(set_commit.pg, "enabled", lambda: False)
    monkeypatch.setattr(set_commit, "insert_memory_versions", insert)
    monkeypatch.setattr(set_commit, "_lift_above_deletes", AsyncMock())
    monkeypatch.setattr(set_commit, "_lift_after_insert", AsyncMock(return_value=7))
    monkeypatch.setattr(set_commit, "_announce_set", AsyncMock())
    monkeypatch.setattr(set_commit, "fence_high", AsyncMock())
    monkeypatch.setattr(set_commit, "_entity_projection_tail", lambda *a, **kw: [])
    monkeypatch.setattr(set_commit, "project_stated_relations", AsyncMock())
    monkeypatch.setattr(set_commit, "count_write", lambda *a: None)
    monkeypatch.setattr("server.memory.store.tombstone_vectors_of", AsyncMock())
    monkeypatch.setattr(audit, "emit", emit)
    return writes, receipts


def _write(*, kind: str = "episodic", high: bool = False, dry_run: bool = False) -> Any:
    input = MemorySetInput(
        org="test",
        namespace="private",
        agent="writer",
        memory_key="decision",
        memory_type=kind,
        sensitivity="high" if high else "low",
        value=_SECRET,
        pinned=True,
        dry_run=dry_run,
    )
    w = set_commit._SetWrite(
        input=input, version=7, retyped_from="", existing=None, expires_at=None
    )
    w.memory_id = "33333333-3333-3333-3333-333333333333"
    w.new_sha = sha256_value(input.value)
    w.source_class = "external"
    w.writer_actor = "service:writer"
    w.memory_row = {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "user_id": input.user_id,
        "session_id": input.session_id,
        "memory_key": input.memory_key,
        "memory_id": w.memory_id,
        "version": 7,
        "sensitivity": "high" if high else "low",
    }
    return w


@pytest.mark.parametrize("kind,high", [("episodic", False), ("profile", False), ("semantic", True)])
async def test_plain_ch_write_emits_one_hash_only_receipt(
    plain: Any, kind: str, high: bool
) -> None:
    writes, receipts = plain
    w = _write(kind=kind, high=high)
    out = await set_commit._set_plain_insert(w, object())
    assert out.version == 7
    assert writes == [w.memory_row]
    assert receipts == [
        {
            "event_type": "memory.set",
            "org": w.input.org,
            "namespace": w.input.namespace,
            "actor": w.writer_actor,
            "subject": {
                "memory_key": w.input.memory_key,
                "memory_id": w.memory_id,
                "content_sha256": sha256_value(_SECRET),
                "source_class": "external",
                "verdict": "",
                "pinned": True,
            },
        }
    ]
    assert _SECRET not in repr(receipts)


@pytest.mark.parametrize("postgres", [False, True])
async def test_plain_dry_run_has_no_receipt(plain: Any, monkeypatch: Any, postgres: bool) -> None:
    writes, receipts = plain
    commit = AsyncMock()
    monkeypatch.setattr(set_commit.pg, "enabled", lambda: postgres)
    monkeypatch.setattr(set_commit, "commit_set_rows", commit)
    out = await set_commit._set_plain_insert(_write(dry_run=True), object())
    assert out.dry_run and out.noop and out.version == 0
    assert writes == receipts == []
    commit.assert_not_awaited()


async def test_refused_plain_insert_has_no_receipt(plain: Any, monkeypatch: Any) -> None:
    writes, receipts = plain
    monkeypatch.setattr(
        set_commit, "insert_memory_versions", AsyncMock(side_effect=PermissionError("refused"))
    )
    with pytest.raises(PermissionError, match="refused"):
        await set_commit._set_plain_insert(_write(), object())
    assert writes == receipts == []


async def test_plain_audit_failure_does_not_fail_write(plain: Any, monkeypatch: Any) -> None:
    emit = AsyncMock(side_effect=RuntimeError("audit unavailable"))
    monkeypatch.setattr(audit, "emit", emit)
    out = await set_commit._set_plain_insert(_write(), object())
    assert out.version == 7
    emit.assert_awaited_once()


async def test_plain_receipt_survives_later_lift_failure(plain: Any, monkeypatch: Any) -> None:
    writes, receipts = plain
    monkeypatch.setattr(
        set_commit, "_lift_after_insert", AsyncMock(side_effect=RuntimeError("lift unavailable"))
    )
    with pytest.raises(RuntimeError, match="lift unavailable"):
        await set_commit._set_plain_insert(_write(), object())
    assert len(writes) == len(receipts) == 1


@pytest.mark.parametrize("high", [False, True])
async def test_plain_pg_commit_already_owns_audit_receipt(
    plain: Any, monkeypatch: Any, high: bool
) -> None:
    writes, receipts = plain
    monkeypatch.setattr(set_commit.pg, "enabled", lambda: True)
    commit = AsyncMock(return_value="committed")
    publish = AsyncMock()
    monkeypatch.setattr(set_commit, "commit_set_rows", commit)
    monkeypatch.setattr(set_commit.pg_publish, "publish", publish)

    @asynccontextmanager
    async def fenced(*args: Any, **kwargs: Any) -> Any:
        answer = CommitAnswer()
        yield answer
        assert answer.committed

    monkeypatch.setattr(set_commit, "fenced_once_committed", fenced)
    monkeypatch.setattr(set_commit, "_answers_at_commit", lambda w: False)
    w, ch = _write(high=high), object()
    out = await set_commit._set_plain_insert(w, ch)
    assert out.version == 7
    commit.assert_awaited_once_with(w, [w.memory_row], MEMORY_ROW | AUDIT_ROW)
    publish.assert_awaited_once_with("committed", ch=ch, wait=True)
    assert writes == receipts == []


@pytest.mark.parametrize("high", [False, True])
async def test_cancelled_audit_still_announces_the_committed_row(plain, monkeypatch, high):
    writes, _ = plain
    entered = asyncio.Event()
    blocked = asyncio.Event()
    finished = False

    async def emit(**kwargs):
        nonlocal finished
        entered.set()
        await blocked.wait()
        finished = True

    monkeypatch.setattr(audit, "emit", emit)
    w = _write(high=high)
    task = asyncio.create_task(set_commit._set_plain_insert(w, object()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "the committed receipt must finish before cancellation returns"
    blocked.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished
    assert writes == [w.memory_row]
    set_commit._announce_set.assert_awaited_once_with(w)
    assert set_commit.fence_high.await_count == int(high)
