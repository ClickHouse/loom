"""A revoke, or a member's removal from a group, written by one process is
enforced by another within the poll interval, through the grant watermark in a
live ClickHouse.

The child process caches what it reads, then reads it in a loop; the parent
makes the change through the same write path a handler uses. The child's TTL
and stale bound are raised, so only a watermark move can explain a denial
inside the window. The window is measured from just before the parent's write
to the child's first denial, in wall time both processes share.

Skips cleanly when LOOM_CH_HOST is unset."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

WINDOW_S = 2.0


def _grant(org: str, revoked: bool) -> dict[str, Any]:
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


async def _admitted(kind: str, org: str) -> bool:
    from server.auth import sharing
    from server.core import grant_cache

    if kind == "grant":
        return bool((await grant_cache.grant_map(org, "alice@example.com")).rank("team"))
    return bool(await sharing.groups_of(org, "user:bob"))


def _replica(kind: str, org: str, ready: Any, changed: Any, clock: Any, out: Any) -> None:
    from server.core import grant_cache
    from server.settings import settings

    settings.grant_cache_ttl_seconds = 600.0
    settings.acl_watermark_max_stale_seconds = 600.0

    async def _main() -> None:
        from server.core.ch import get_client

        await get_client().connect()
        await grant_cache.poll()
        loop = grant_cache.start_poll_loop(settings.acl_watermark_poll_seconds)
        before = await _admitted(kind, org)
        ready.set()
        await asyncio.to_thread(changed.wait, 30)
        denied_at = None
        while time.time() - clock.value < 10:
            if not await _admitted(kind, org):
                denied_at = time.time()
                break
            await asyncio.sleep(0.05)
        out.put((before, None if denied_at is None else denied_at - clock.value))
        loop.cancel()

    asyncio.run(_main())


async def _connected(work: Any) -> None:
    from server.core.ch import get_client

    await get_client().connect()
    try:
        await work()
    finally:
        await get_client().disconnect()


def _run_change(kind: str, org: str, step: str) -> None:
    from server.identity.app import scim_groups
    from server.identity.store import grants as grants_repo

    async def _work() -> None:
        if kind == "grant":
            await grants_repo.insert_row(_grant(org, revoked=step == "change"))
            return
        members = ["user:bob"] if step == "setup" else []
        row = scim_groups.group_row(
            org, "readers", display_name="Readers", external_id="", members=members
        )
        await scim_groups._write(row, event="scim.group_changed")

    asyncio.run(_connected(_work))


@pytest.mark.parametrize("kind", ["grant", "group"])
def test_another_process_denies_within_the_window(kind: str) -> None:
    org = unique_org("watermark")
    _run_change(kind, org, "setup")
    ctx = mp.get_context("spawn")
    ready, changed, out = ctx.Event(), ctx.Event(), ctx.Queue()
    clock = ctx.Value("d", 0.0)
    child = ctx.Process(target=_replica, args=(kind, org, ready, changed, clock, out))
    child.start()
    try:
        assert ready.wait(60), "the replica never cached the state"
        clock.value = time.time()
        _run_change(kind, org, "change")
        changed.set()
        before, elapsed = out.get(timeout=30)
    finally:
        child.join(timeout=30)
        if child.is_alive():
            child.kill()
    assert before, "the replica admitted before the change"
    assert elapsed is not None and elapsed < WINDOW_S, elapsed
