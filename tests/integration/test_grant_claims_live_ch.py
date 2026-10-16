"""Grant changes kept apart by a real lease on ClickHouse (a Keeper key, or the
row queue without a Keeper).

The unit tests drive `grant_claims` with claims in process memory; this takes
them on ClickHouse, so two changes of one grant wait on the lease store.
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid

import pytest

from server.auth import grant_claims
from server.core import leases
from server.core.ch import CHClient

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.asyncio
async def test_two_changes_of_one_grant_are_kept_apart_on_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = CHClient()
    await c.connect()
    monkeypatch.setattr(grant_claims, "take", functools.partial(leases.take, ch=c))
    try:
        await _kept_apart()
    finally:
        await c.disconnect()


async def _kept_apart() -> None:
    key = ("it-grant-claims", f"ns-{uuid.uuid4().hex[:8]}", "alice")
    log: list[str] = []
    entered = asyncio.Event()

    async def _change(name: str, hold: float) -> None:
        async with grant_claims.grant_change(*key):
            log.append(f"{name} in")
            entered.set()
            await asyncio.sleep(hold)
            log.append(f"{name} out")

    first = asyncio.create_task(_change("a", 1.5))
    await entered.wait()
    await asyncio.gather(first, _change("b", 0))
    assert log == ["a in", "a out", "b in", "b out"]
    started = time.monotonic()
    await _change("c", 0)
    assert time.monotonic() - started < 5, "both claims were released"
