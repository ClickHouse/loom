"""Fixtures the same-key and graph tests share."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration._resources import unique_org


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client
    from server.memory.app import after_write, graph_write_marks, intent_marks

    c = get_client()
    await c.connect()
    yield c
    # What the test's writes left running finishes here, on this client: left to run on,
    # it lands inside the next test and on a client that test reconnects.
    await after_write.drain(timeout_s=60.0)
    await intent_marks.drain()
    await graph_write_marks.drain(timeout_s=60.0)
    assert after_write.pending() == 0, "a side effect was still running after 60 s"
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
