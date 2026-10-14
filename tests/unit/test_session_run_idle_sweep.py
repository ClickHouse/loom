"""The idle sweep closes an MCP session's implicit run from the ledger alone.
Which runs are idle is one ClickHouse query (live in
tests/integration/test_session_run_ledger_live_ch.py); here: a pass closes what
the query returns until its time is spent, and one run whose close fails does
not hold up the others."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.tasks.app import run as run_handlers


class _Ledger:
    """Answers the idle query with `run_ids`; each step yields to the loop, as a
    ClickHouse round trip does."""

    def __init__(self, *run_ids: str) -> None:
        self.rows = [{"org": "o", "namespace": "n", "run_id": r} for r in run_ids]
        self.completes: list[Any] = []
        self.params: list[Any] = []

    def _done(self, run_id: str) -> bool:
        return run_id in {c.run_id for c in self.completes}

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        assert "WITH candidates AS" in sql and "ORDER BY rand()" in sql
        self.params.append(params)
        return self.rows[: params["lim"]]

    async def active(self, org: str, run_id: str) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        return [r for r in self.rows if r["run_id"] == run_id and not self._done(run_id)]

    async def fetch(self, ch: Any, org: str, ns: str, run_id: str) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return None if self._done(run_id) else {"status": "active", "metadata": "{}"}

    async def complete(self, ch: Any, inp: Any, existing: Any) -> None:
        await asyncio.sleep(0.01)
        if inp.run_id.startswith("broken"):
            raise ValueError("the row is gone")
        self.completes.append(inp)


def _ledger(monkeypatch, *run_ids: str) -> _Ledger:
    ledger = _Ledger(*run_ids)
    monkeypatch.setattr(run_handlers, "get_client", lambda: ledger)
    monkeypatch.setattr(run_handlers, "_complete", ledger.complete)
    monkeypatch.setattr(run_handlers, "active_session_runs", ledger.active)
    monkeypatch.setattr(run_handlers.runs_store, "fetch_run", ledger.fetch)
    monkeypatch.setattr(run_handlers.runs_store, "called_lately", AsyncMock(return_value=False))
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

    async def slow_complete(ch: Any, inp: Any, existing: Any) -> None:
        await asyncio.sleep(0.2)  # a close on a slow ClickHouse
        ledger.completes.append(inp)

    monkeypatch.setattr(run_handlers, "_complete", slow_complete)
    monkeypatch.setattr(run_handlers, "IDLE_CLOSE_BUDGET_S", 0.1)
    monkeypatch.setattr(run_handlers, "IDLE_CLOSES_AT_ONCE", 1)
    assert await run_handlers.close_idle_session_runs(1800) == 1
    assert [c.run_id for c in ledger.completes] == ["slow"]  # "next" waits for the next pass


@pytest.mark.asyncio
async def test_a_pass_closes_a_few_runs_at_a_time(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, *(f"r{i}" for i in range(10)))
    running, most = 0, 0

    async def complete(ch: Any, inp: Any, existing: Any) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        ledger.completes.append(inp)

    monkeypatch.setattr(run_handlers, "_complete", complete)
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


def test_the_pass_deadline_leaves_room_for_the_close_budget() -> None:
    from server.workers.registry import _WORKERS

    (row,) = [w for w in _WORKERS if w.name == "mcp_session_run_sweep"]
    assert row.max_pass_seconds is not None
    assert row.max_pass_seconds >= 2 * run_handlers.IDLE_CLOSE_BUDGET_S


@pytest.mark.asyncio
async def test_closes_racing_for_one_run_judge_nothing(monkeypatch) -> None:
    """Two sweeps and a DELETE at once: any that get through write the same
    close, with no outcome and the memories kept, so a repeat changes nothing."""
    ledger = _ledger(monkeypatch, "a")
    await asyncio.gather(
        run_handlers.close_idle_session_runs(1800),
        run_handlers.close_idle_session_runs(1800),
        run_handlers.close_session_run("o", "a", reason="session_end"),
    )
    assert ledger.completes and {c.run_id for c in ledger.completes} == {"a"}
    assert {(c.outcome, c.retain) for c in ledger.completes} == {("none", "keep")}


@pytest.mark.asyncio
async def test_a_run_its_token_called_since_the_pick_is_left_open(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, "a")
    monkeypatch.setattr(run_handlers.runs_store, "called_lately", AsyncMock(return_value=True))
    assert await run_handlers.close_idle_session_runs(1800) == 0
    assert ledger.completes == []
