"""The graph fences read `loom.memory`: under the Postgres authority, where a write answers
at its commit, a fence in that write's request reads once its publication has landed, so
the write is not judged to have lost its value before its flush. No other wait."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.memory.app import graph_holders
from server.memory.domain import triples_follow_rows as tfr
from server.memory.store import pg_batch

_TRIPLE: tfr.Triple = ("alice", "meet", "bob")


class _CH:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.order.append("read")
        return []


_FENCES = [
    lambda ch: graph_holders.versions_held("o", "n", [("m", "sha")], ch=ch),
    lambda ch: graph_holders.edge_holders("o", "n", [("m", "sha")], ch=ch),
    lambda ch: graph_holders.live_holders("o", "n", [_TRIPLE], ch=ch),
    lambda ch: graph_holders.held_by_unlinked_rows("o", "n", [_TRIPLE], ch=ch),
]
_IDS = ["versions_held", "edge_holders", "live_holders", "held_by_unlinked_rows"]


@pytest.mark.parametrize("read", _FENCES, ids=_IDS)
async def test_a_fence_reads_once_its_writes_publication_lands(read: Any) -> None:
    order: list[str] = []
    published = asyncio.get_running_loop().create_future()
    pg_batch._own_write.set((published, time.monotonic() + 30))
    fence = asyncio.ensure_future(read(_CH(order)))
    await asyncio.sleep(0)
    assert order == []
    published.set_result(None)
    await fence
    assert order and set(order) == {"read"}


@pytest.mark.parametrize("read", _FENCES, ids=_IDS)
async def test_a_fence_with_no_write_of_its_own_pending_reads_at_once(read: Any) -> None:
    order: list[str] = []
    await read(_CH(order))
    assert order and set(order) == {"read"}


async def test_a_fence_waits_no_longer_than_its_writes_lease() -> None:
    """A publication that has not landed by its lease's end is the delivery worker's: the
    fence then reads what ClickHouse holds, as on a write whose publication is pending."""
    order: list[str] = []
    never = asyncio.get_running_loop().create_future()
    pg_batch._own_write.set((never, time.monotonic() + 0.01))
    assert await graph_holders.versions_held("o", "n", [("m", "sha")], ch=_CH(order)) == set()
    assert order == ["read"]


async def test_a_write_handed_off_holds_its_fences_until_its_flush_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real path: a write published without waiting records itself for its request,
    and a fence awaited in that task, or in a task it schedules, reads only once the
    flush has landed it."""
    from tests.unit.test_pg_batch import _Batching

    batch = _Batching(monkeypatch)
    batch.inserting.clear()
    from server.memory.store import pg_publish

    await pg_publish.publish(batch.committed("k", 1), ch=batch.ch, wait=False)
    order: list[str] = []
    scheduled = asyncio.get_running_loop().create_task(
        graph_holders.edge_holders("o", "n", [("m", "sha")], ch=_CH(order))
    )
    await asyncio.sleep(0)
    assert order == []
    asyncio.get_running_loop().call_soon(batch.inserting.set)
    # In the very task that published it.
    await graph_holders.versions_held("o", "n", [("m", "sha")], ch=_CH(order))
    await scheduled
    assert order == ["read", "read"]
