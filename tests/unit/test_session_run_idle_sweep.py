"""The idle sweep closes an MCP session's implicit run from the ledger alone.
Which runs are idle is one ClickHouse query (live in
tests/integration/test_session_run_ledger_live_ch.py); here: a pass closes what
the query returns until its time is spent, and one run whose close fails does
not hold up the others."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.tasks.app import run as run_handlers


class _Ledger:
    """Answers the idle query with `run_ids`; records the closes."""

    def __init__(self, *run_ids: str) -> None:
        self.rows = [{"org": "o", "namespace": "n", "run_id": r} for r in run_ids]
        self.completes: list[Any] = []
        self.params: list[Any] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        assert "WITH candidates AS" in sql and "ORDER BY rand()" in sql
        self.params.append(params)
        return self.rows[: params["lim"]]

    async def complete(self, inp: Any) -> None:
        if inp.run_id.startswith("broken"):
            raise ValueError("the row is gone")
        self.completes.append(inp)


def _ledger(monkeypatch, *run_ids: str) -> _Ledger:
    ledger = _Ledger(*run_ids)
    monkeypatch.setattr(run_handlers, "get_client", lambda: ledger)
    monkeypatch.setattr(run_handlers, "handle_run_complete", ledger.complete)
    return ledger


@pytest.mark.asyncio
async def test_an_idle_run_is_completed_with_outcome_none(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, "a")
    assert await run_handlers.close_idle_session_runs(1800, org="o") == 1
    assert ledger.params == [{"org": "o", "idle": 1800, "lim": 500}]
    (c,) = ledger.completes
    assert (c.org, c.namespace, c.run_id) == ("o", "n", "a")
    assert (c.status, c.outcome, c.metadata) == ("completed", "none", {"closed_by": "mcp_idle"})


@pytest.mark.asyncio
async def test_a_run_whose_close_fails_does_not_hold_up_the_rest(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, "broken", "next")
    assert await run_handlers.close_idle_session_runs(1800) == 1
    assert [c.run_id for c in ledger.completes] == ["next"]


@pytest.mark.asyncio
async def test_a_pass_stops_closing_when_its_time_is_spent(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, "slow", "next")

    async def slow_complete(inp: Any) -> None:
        await asyncio.sleep(0.2)  # a close on a slow ClickHouse
        ledger.completes.append(inp)

    monkeypatch.setattr(run_handlers, "handle_run_complete", slow_complete)
    monkeypatch.setattr(run_handlers, "IDLE_CLOSE_BUDGET_S", 0.1)
    monkeypatch.setattr(run_handlers, "IDLE_CLOSES_AT_ONCE", 1)
    assert await run_handlers.close_idle_session_runs(1800) == 1
    assert [c.run_id for c in ledger.completes] == ["slow"]  # "next" waits for the next pass


@pytest.mark.asyncio
async def test_a_pass_closes_a_few_runs_at_a_time(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, *(f"r{i}" for i in range(10)))
    running, most = 0, 0

    async def complete(inp: Any) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        ledger.completes.append(inp)

    monkeypatch.setattr(run_handlers, "handle_run_complete", complete)
    monkeypatch.setattr(run_handlers, "IDLE_CLOSES_AT_ONCE", 3)
    assert await run_handlers.close_idle_session_runs(1800) == 10
    assert most == 3


@pytest.mark.asyncio
async def test_the_worker_pass_closes_with_the_idle_window(monkeypatch) -> None:
    """The `mcp_session_run_sweep` worker's pass reads the window from settings."""
    from server.settings import settings
    from server.tasks.workers import session_run_sweep

    windows: list[int] = []

    async def close(idle_seconds: int) -> int:
        windows.append(idle_seconds)
        return 2

    monkeypatch.setattr(settings, "mcp_session_run_idle_seconds", 900)
    monkeypatch.setattr(run_handlers, "close_idle_session_runs", close)
    assert await session_run_sweep.run_pass() == 2
    assert windows == [900]
