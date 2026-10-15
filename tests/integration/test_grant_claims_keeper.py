"""Grant changes kept apart by a real KeeperMap table (`loom.grant_claims`).

The unit tests drive `grant_claims` without a Keeper; this runs the claim on
ClickHouse, so two changes of one grant in one process are kept apart by
Keeper, not by the in-process lock. Skips without a Keeper.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.auth import grant_claims
from server.core.repo_base import _default_client


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    client = _default_client()
    await client.connect()
    try:
        grant_claims.CLAIMS.atomic = None
        if not await grant_claims.CLAIMS.available(client):
            pytest.skip("needs loom.grant_claims (a ClickHouse Keeper)")
        yield client
    finally:
        grant_claims.CLAIMS.atomic = None
        await client.disconnect()


async def test_two_changes_of_one_grant_are_kept_apart_by_keeper(ch: Any) -> None:
    key = ("it-grant-claims", f"ns-{uuid.uuid4().hex[:8]}", "alice")
    log: list[str] = []

    async def _change(name: str, hold: float) -> None:
        async with grant_claims.grant_change(*key):
            log.append(f"{name} in")
            await asyncio.sleep(hold)
            log.append(f"{name} out")

    first = asyncio.create_task(_change("a", 0.3))
    await asyncio.sleep(0.1)
    await asyncio.gather(first, _change("b", 0))
    assert log == ["a in", "a out", "b in", "b out"]
    rows = await ch.query_all(
        "SELECT claim_key FROM loom.grant_claims WHERE claim_key LIKE '%it-grant-claims%'"
    )
    assert rows == [], "both claims were released"
