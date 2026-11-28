"""A list page says how many rows it read, so a client pages past rows the authority dropped."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.api.models_crud import MemoryListInput
from server.memory.app import query as mq


@pytest.mark.asyncio
async def test_a_page_of_dropped_rows_still_says_how_many_it_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def nsv(org: str, ns: str) -> int:
        return 0

    async def listed(*a: Any, **k: Any) -> list[Any]:
        return [object()] * (k["limit"])  # one more than asked: more rows exist

    async def served(rows: list[Any]) -> list[Any]:
        return []  # every row of the page was deleted or rewritten, not yet published

    async def count(identity: Any, **k: Any) -> int:
        return 5

    monkeypatch.setattr(memory_repo, "namespace_version", nsv)
    monkeypatch.setattr(memory_repo, "list_by_namespace", listed)
    monkeypatch.setattr(memory_repo, "served_rows", served)
    monkeypatch.setattr(memory_repo, "count_by_namespace", count)
    mq.reset_list_counts_cache()
    out = await mq._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="*", limit=2, offset=2)
    )
    assert (out.entries, out.truncated, out.rows_read) == ([], True, 2)
