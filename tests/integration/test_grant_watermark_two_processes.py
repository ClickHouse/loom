"""A revoke written by one process is enforced by another within the poll
interval, through the grant watermark in a live ClickHouse.

The child process caches a subject's grant, then reads it in a loop; the parent
revokes it through the grants store, as a handler does. Without the watermark
the child would keep the grant for `grant_cache_ttl_seconds`; the TTL is raised
here so only the watermark can explain a fast denial.

Skips cleanly when LOOM_CH_HOST is unset."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import time
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

WINDOW_S = 2.0


def _row(org: str, revoked: bool) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "org": org,
        "namespace": "team",
        "subject": "alice@example.com",
        "role": "writer",
        "granted_by": "test",
        "revoked_at": datetime.now(UTC).replace(tzinfo=None) if revoked else None,
        "version": time.time_ns(),
        "status": "active",
    }


def _replica(org: str, ready: Any, revoked: Any, out: Any) -> None:
    from server.core import grant_cache
    from server.settings import settings

    settings.grant_cache_ttl_seconds = 600.0

    async def _main() -> None:
        from server.core.ch import get_client

        await get_client().connect()
        await grant_cache.poll()
        loop = grant_cache.start_poll_loop(settings.acl_watermark_poll_seconds)
        first = await grant_cache.grant_map(org, "alice@example.com")
        ready.set()
        revoked.wait(30)
        start = time.monotonic()
        while time.monotonic() - start < 10:
            grants = await grant_cache.grant_map(org, "alice@example.com")
            if not grants.rank("team"):
                out.put((first.rank("team"), time.monotonic() - start))
                break
            await asyncio.sleep(0.05)
        else:
            out.put((first.rank("team"), None))
        loop.cancel()

    asyncio.run(_main())


def test_another_process_denies_a_revoked_grant_within_the_window() -> None:
    from server.core import grant_cache
    from server.identity.store import grants as grants_repo

    org = f"itest-wm-{uuid.uuid4().hex[:8]}"

    async def _write(revoked: bool) -> None:
        from server.core.ch import get_client

        await get_client().connect()
        try:
            await grants_repo.insert_row(_row(org, revoked))
            await asyncio.gather(*grant_cache._announcing)
        finally:
            await get_client().disconnect()

    asyncio.run(_write(revoked=False))
    ctx = mp.get_context("spawn")
    ready, revoked, out = ctx.Event(), ctx.Event(), ctx.Queue()
    child = ctx.Process(target=_replica, args=(org, ready, revoked, out))
    child.start()
    try:
        assert ready.wait(60), "the replica never cached the grant"
        asyncio.run(_write(revoked=True))
        revoked.set()
        first_rank, elapsed = out.get(timeout=30)
    finally:
        child.join(timeout=30)
        if child.is_alive():
            child.kill()
    assert first_rank, "the replica saw the grant before the revoke"
    assert elapsed is not None and elapsed < WINDOW_S, elapsed
