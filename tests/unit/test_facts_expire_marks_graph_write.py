"""facts.expire_by_adapter deletes triples with an ALTER mutation, which no INSERT-side
bookkeeping sees; it must mark the namespace's graph written, or facts.traverse keeps
serving a walk remembered before the delete for its full minute."""

from __future__ import annotations

from typing import Any

import pytest


class _CH:
    def __init__(self, t: int, tm: int) -> None:
        self.row = {"t_count": t, "tm_count": tm}
        self.executed: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, int]:
        return self.row

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.executed.append(sql)


@pytest.fixture
def local_marks() -> Any:
    """The graph-marks module; the suite's autouse teardown resets it after each test."""
    from server.memory.app import graph_write_marks

    return graph_write_marks


async def _expire(monkeypatch: pytest.MonkeyPatch, ch: _CH) -> None:
    from server.memory.api.models_facts import FactsExpireByAdapterInput
    from server.memory.app import facts

    monkeypatch.setattr(facts, "get_client", lambda: ch)
    await facts.handle_facts_expire_by_adapter(
        FactsExpireByAdapterInput(org="acme", namespace="ns", adapter_name="ad", max_confidence=0.5)
    )


@pytest.mark.asyncio
async def test_expiring_triples_marks_the_graph_written(monkeypatch, local_marks) -> None:
    ch = _CH(3, 2)
    await _expire(monkeypatch, ch)
    assert len(ch.executed) == 2
    assert ("acme", "ns") in local_marks._unrecorded


@pytest.mark.asyncio
async def test_nothing_to_expire_leaves_the_graph_unmarked(monkeypatch, local_marks) -> None:
    ch = _CH(0, 0)
    await _expire(monkeypatch, ch)
    assert ch.executed == []
    assert local_marks._unrecorded == set()
