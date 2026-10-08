"""The question window, handler path: the window is resolved on every search and
reported in retrieval_work."""

from __future__ import annotations

import pytest

from tests.unit.test_search_temporal_prongs import (  # noqa: F401  (fixture re-use)
    _input,
    search_env,
)


@pytest.mark.asyncio
async def test_window_is_reported(search_env) -> None:  # noqa: F811
    from server.memory.app.search import handle_memory_search

    out = await handle_memory_search(
        _input(query="What did Caroline do in May 2023?", observation_date="2023-06-01")
    )
    assert out.retrieval_work.get("question_window") == "month:2023-05-01..2023-05-31"


@pytest.mark.asyncio
async def test_no_window_leaves_fields_empty(search_env) -> None:  # noqa: F811
    from server.memory.app.search import handle_memory_search

    out = await handle_memory_search(_input(query="favourite colour of the user"))
    assert "question_window" not in out.retrieval_work
