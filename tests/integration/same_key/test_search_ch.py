"""One key live in two sessions: a search serves each session's row with its own text."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

NS = "same-key-search"


async def _two_sessions(org: str) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    for session, value in (
        ("s1", "User's personal best 5k time is 27:10"),
        ("s2", "User's personal best marathon time is 3:41:00"),
    ):
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id="u",
                session_id=session,
                memory_key="user_pb",
                value=value,
            )
        )


@pytest.mark.parametrize("bodies", ["excerpt", "unit"])
async def test_each_session_row_of_one_key_is_served_with_its_own_text(
    ch: Any, org: str, bodies: str
) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search

    await _two_sessions(org)
    out = await handle_memory_search(
        MemorySearchInput(
            org=org, namespace=NS, agent="*", query="personal best time", bodies=bodies, top_k=10
        )
    )
    served = {h.session_id: h.content_excerpt for h in out.results if h.memory_key == "user_pb"}
    assert set(served) == {"s1", "s2"}, out.results
    assert "27:10" in served["s1"] and "3:41:00" in served["s2"]
