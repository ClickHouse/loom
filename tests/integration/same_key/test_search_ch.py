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


async def test_two_users_rows_of_one_key_are_served_with_their_own_text(ch: Any, org: str) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set

    for user, value in (
        ("u1", "User's employer is Acme Corp"),
        ("u2", "User's employer is Globex"),
    ):
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=user,
                memory_key="user_employer",
                value=value,
            )
        )
    out = await handle_memory_search(
        MemorySearchInput(org=org, namespace=NS, agent="*", query="employer", bodies="unit")
    )
    texts = sorted(h.content_excerpt for h in out.results if h.memory_key == "user_employer")
    assert len(texts) == 2 and "Acme" in texts[0] and "Globex" in texts[1], out.results


async def test_one_key_in_many_sessions_leaves_room_for_another_key(ch: Any, org: str) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.search.handler import handle_memory_search

    for n in range(30):
        await handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org,
                namespace=NS,
                agent="bot",
                session_id=f"s{n}",
                entries=[{"memory_key": "worktree_path", "value": f"checkout number {n}"}],
            )
        )
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            entries=[{"memory_key": "worktree_path_owner", "value": "the build team"}],
        )
    )
    out = await handle_memory_search(
        MemorySearchInput(org=org, namespace=NS, agent="*", query="worktree path", top_k=10)
    )
    assert "worktree_path_owner" in {h.memory_key for h in out.results}, out.results
