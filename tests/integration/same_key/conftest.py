"""Fixtures the same-key and graph tests share."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration._resources import unique_org


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
def org() -> str:
    return unique_org("same-key")


@pytest.fixture
def newest_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import projections

    async def _policy(*a: Any, **k: Any) -> str:
        return "newest_wins"

    monkeypatch.setattr(projections._conflicts, "namespace_conflict_policy", _policy)
