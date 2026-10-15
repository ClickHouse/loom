"""A historical search reads the same who and where as a current one: the agent and
namespace_scope defaults reach it before it branches off."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app import temporal_search
from server.memory.app.search import handler


@pytest.mark.asyncio
async def test_a_historical_search_gets_the_resolved_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[MemorySearchInput] = []

    async def _search(input: MemorySearchInput) -> Any:
        seen.append(input)
        return "answered"

    monkeypatch.setattr(temporal_search, "search", _search)
    asked = MemorySearchInput(
        org="o", namespace="n", agent="", query="q", known_as_of=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert await handler.handle_memory_search(asked) == "answered"
    (got,) = seen
    assert got.agent == "*" and got.namespace_scope is not None
