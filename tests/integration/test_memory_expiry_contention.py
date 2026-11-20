"""An expiry scan must yield to a committed write from another client."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from server.core import pg
from server.core.ch import CHClient
from server.core.datetimes import naive_utc_now_seconds
from server.memory import store as memory_repo
from server.memory.app import expiry
from server.memory.store.rows import MemoryRow, build_memory_version_row, identity_key, version_over
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.parametrize("action", expiry.ACTIONS)
@pytest.mark.parametrize("renewed", [False, True])
async def test_expiry_scan_leaves_an_intervening_update(monkeypatch, action, renewed):
    if pg.enabled():
        pytest.skip("exercises the ClickHouse authority")
    now = naive_utc_now_seconds()
    source = MemoryRow(
        memory_id=str(uuid4()),
        org=unique_org("expiry-contention"),
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
        expires_at=now - timedelta(minutes=1),
        on_expire=action,
    )
    first, second = CHClient(), CHClient()
    scanned, resume = asyncio.Event(), asyncio.Event()
    task = None
    key = identity_key(source)
    find = memory_repo.find_expired_candidates
    try:
        await first.connect()
        await second.connect()
        await memory_repo.insert_memory_versions([build_memory_version_row(source)], ch=first)

        async def paused_find(**kw):
            rows = await find(**kw, ch=first)
            assert [identity_key(r) for r in rows] == [key]
            scanned.set()
            await resume.wait()
            return rows

        monkeypatch.setattr(memory_repo, "find_expired_candidates", paused_find)
        monkeypatch.setattr("server.core.ch.get_client", lambda: first)
        task = asyncio.create_task(
            expiry.run_pass(
                expiry.ExpiryConfig(org_filter=source.org, namespace_filter=source.namespace)
            )
        )
        async with asyncio.timeout(30):
            await scanned.wait()
            update = build_memory_version_row(
                source,
                value="rejected",
                expires_at=None if renewed else source.expires_at,
                version=version_over(source),
            )
            await memory_repo.insert_memory_versions([update], ch=second)
            committed = (
                await memory_repo.memory_rows_by_identity_keys([key], ch=second, sequential=True)
            )[key]
            resume.set()
            result = await task
        final = (await memory_repo.memory_rows_by_identity_keys([key], ch=first, sequential=True))[
            key
        ]
        assert final.model_dump() == committed.model_dump()
        assert final.value == "rejected"
        assert result.moved_skipped == 1 and not result.failures
        assert (result.tombstoned, result.demoted, result.anonymized) == (0, 0, 0)
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
