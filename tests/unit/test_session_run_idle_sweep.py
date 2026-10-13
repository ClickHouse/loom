"""The idle sweep closes an MCP session's implicit run from the ledger alone.
The query keeps a run its own token called lately (live on ClickHouse in
tests/integration/test_session_run_ledger_live_ch.py); here: one pass closes at
most its limit, and one run whose close fails does not hold up the others."""

from __future__ import annotations

from typing import Any

import pytest

from server.tasks.app import run as run_handlers


class _Ledger:
    def __init__(self, *run_ids: str) -> None:
        self.rows = [{"org": "o", "namespace": "n", "run_id": r} for r in run_ids]
        self.params: list[Any] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        assert "FROM loom.agent_runs FINAL" in sql and "NOT IN" in sql
        self.params.append(params)
        return self.rows[: params["lim"]]


@pytest.fixture
def completes(monkeypatch) -> list[Any]:
    done: list[Any] = []

    async def complete(inp: Any) -> None:
        if inp.run_id == "broken":
            raise ValueError("the row is gone")
        done.append(inp)

    monkeypatch.setattr(run_handlers, "handle_run_complete", complete)
    return done


@pytest.mark.asyncio
async def test_an_idle_run_is_completed_with_outcome_none(monkeypatch, completes) -> None:
    ledger = _Ledger("a", "b")
    monkeypatch.setattr(run_handlers, "get_client", lambda: ledger)
    assert await run_handlers.close_idle_session_runs(1800, limit=1, org="o") == 1
    assert ledger.params == [{"org": "o", "idle": 1800, "lim": 1}]
    (c,) = completes
    assert (c.org, c.namespace, c.run_id) == ("o", "n", "a")
    assert (c.status, c.outcome, c.metadata) == ("completed", "none", {"closed_by": "mcp_idle"})


@pytest.mark.asyncio
async def test_a_run_whose_close_fails_does_not_hold_up_the_rest(monkeypatch, completes) -> None:
    monkeypatch.setattr(run_handlers, "get_client", lambda: _Ledger("broken", "next"))
    assert await run_handlers.close_idle_session_runs(1800) == 1
    assert [c.run_id for c in completes] == ["next"]


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
