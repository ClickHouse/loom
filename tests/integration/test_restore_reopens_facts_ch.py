"""Undoing a delete brings back the facts the row states: while it was deleted another
row's revision may have closed them, as no live row held them any longer."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from tests.integration._resources import unique_org

NS = "restore-facts"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


async def _set(org: str, key: str, value: str, relations: list) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    await handle_memory_set(
        MemorySetInput(
            org=org, namespace=NS, agent="bot", memory_key=key, value=value, relations=relations
        )
    )


async def _paris_open(ch: Any, org: str, want: bool) -> bool:
    # Closes are mutations: poll up to ten seconds for `want`.
    for _ in range(50):
        rows = await ch.query_all(
            "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
            " AND object = 'paris' AND valid_to IS NULL"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        if (int(rows[0]["n"]) > 0) == want:
            break
        await asyncio.sleep(0.2)
    return int(rows[0]["n"]) > 0


async def test_undoing_a_delete_reopens_the_facts_the_row_states(ch) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput, MemoryRestoreInput
    from server.memory.app.handlers import handle_memory_delete
    from server.memory.app.restore import handle_memory_restore

    org = unique_org("restore-facts")
    fact = ("user", "live_in", "paris")
    a, b = f"a-{uuid.uuid4().hex[:6]}", f"b-{uuid.uuid4().hex[:6]}"
    await _set(org, a, "I live in Paris", [fact])
    await _set(org, b, "Home for me is the city of Paris, France", [fact])
    await handle_memory_delete(MemoryDeleteInput(org=org, namespace=NS, agent="bot", memory_key=a))
    # B still states it, so it stays open; B's revision drops it, and nothing holds it.
    assert await _paris_open(ch, org, want=True)
    await _set(org, b, "Weekends go to sailing lessons on the lake", [])
    assert not await _paris_open(ch, org, want=False)

    undone = await handle_memory_restore(
        MemoryRestoreInput(org=org, namespace=NS, agent="bot", memory_key=a)
    )
    assert undone.restored
    assert await _paris_open(ch, org, want=True)
