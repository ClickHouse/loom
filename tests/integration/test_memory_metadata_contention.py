"""Two ClickHouse clients exercise metadata copies over an intervening write."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from server.core import pg
from server.core.ch import CHClient
from server.core.datetimes import naive_utc_now_seconds
from server.core.hashing import served_content_sha256
from server.memory.api.models_crud import MemoryVerifyInput
from server.memory.api.models_lock import MemoryLockInput, MemoryUnlockInput
from server.memory.app import lock, verify
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.pg_memory import AdmissionContendedError
from server.memory.store.reads import memory_rows_by_identity_keys
from server.memory.store.rows import MemoryRow, build_memory_version_row, identity_key, version_over
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["lock", "unlock", "verify", "revoke"])
async def test_metadata_copy_refuses_a_committed_intervening_update(monkeypatch, operation):
    if pg.enabled():
        pytest.skip("exercises the ClickHouse authority")
    now = naive_utc_now_seconds()
    source = MemoryRow(
        memory_id=str(uuid4()),
        org=unique_org("metadata-copy"),
        namespace="notes",
        agent="writer",
        user_id="-",
        session_id="-",
        memory_key="review",
        memory_type="episodic",
        value="proposed",
        confidence=0.8,
        version=version_over(0),
        pinned=False,
        created_at=now,
        updated_at=now,
        last_reinforced_at=now,
        expires_at=now + timedelta(days=1),
        lock_mode="no_delete" if operation == "unlock" else "",
        verified_by="human:reviewer" if operation == "revoke" else "",
        verified_sha256=served_content_sha256("proposed") if operation == "revoke" else "",
        verified_at=now if operation == "revoke" else None,
    )
    first, second = CHClient(), CHClient()
    paused, resume = asyncio.Event(), asyncio.Event()
    task = None
    mirrors = []
    key = identity_key(source)
    try:
        await first.connect()
        await second.connect()
        await insert_memory_versions([build_memory_version_row(source)], ch=first)

        async def captured_head(*args, **kw):
            head = (await memory_rows_by_identity_keys([key], ch=first, sequential=True))[key]
            paused.set()
            await resume.wait()
            return head

        monkeypatch.setattr(lock, "_head", captured_head)
        monkeypatch.setattr(verify, "_resolve_row", captured_head)
        monkeypatch.setattr(lock, "get_client", lambda: first)
        monkeypatch.setattr(verify, "get_client", lambda: first)
        monkeypatch.setattr(verify, "_schedule_mirror", lambda *args, **kw: mirrors.append(kw))
        ident = dict(
            org=source.org,
            namespace=source.namespace,
            agent=source.agent,
            memory_key=source.memory_key,
        )
        if operation == "lock":
            call = lock.handle_memory_lock(MemoryLockInput(**ident, reason="hold"))
        elif operation == "unlock":
            call = lock.handle_memory_unlock(MemoryUnlockInput(**ident))
        else:
            call = verify.handle_memory_verify(
                MemoryVerifyInput(
                    org=source.org,
                    memory_id=source.memory_id,
                    actor="human:reviewer",
                    revoke=operation == "revoke",
                )
            )
        task = asyncio.create_task(call)
        async with asyncio.timeout(30):
            await paused.wait()
            update = build_memory_version_row(
                source, value="rejected", version=version_over(source)
            )
            await insert_memory_versions([update], ch=second)
            committed = (await memory_rows_by_identity_keys([key], ch=second, sequential=True))[key]
            resume.set()
            with pytest.raises(AdmissionContendedError):
                await task
        final = (await memory_rows_by_identity_keys([key], ch=first, sequential=True))[key]
        assert final.model_dump() == committed.model_dump()
        assert final.value == "rejected"
        assert mirrors == []
        history = await first.query_one(
            "SELECT count() AS n FROM loom.memory_versions WHERE org = {org:String}",
            {"org": source.org},
        )
        assert history["n"] == 2
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await first.disconnect()
        await second.disconnect()
