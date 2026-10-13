"""The idle sweep closes an MCP session's implicit run from the ledger alone:
only a recent call of the run's own token keeps it open, and one run whose
close fails does not hold up the others."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now
from server.tasks.app import run as run_handlers

IDLE = 1800.0


def _row(run_id: str, subject: str) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "run_id": run_id,
        "started_at": naive_utc_now() - timedelta(hours=2),
        "metadata": json.dumps({"implicit": True, "subject": subject}),
    }


class _Ledger:
    def __init__(self, runs: list[dict[str, Any]], calls: dict[tuple[str, str], Any]) -> None:
        self.runs, self.calls = runs, calls
        self.tables: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.tables.append("agent_runs" if "agent_runs" in sql else "agent_events")
        if "agent_runs" in sql:
            assert "status = 'active'" in sql and params["pattern"] == "^mcp-[0-9a-f]{40}$"
            return self.runs
        assert params["rids"] == [r["run_id"] for r in self.runs]
        return [{"run_id": r, "subject": s, "last": t} for (r, s), t in self.calls.items()]


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
async def test_only_the_runs_own_recent_calls_keep_it_open(monkeypatch, completes) -> None:
    now = naive_utc_now()
    ledger = _Ledger(
        [_row("busy", "sa:t"), _row("quiet", "sa:t"), _row("borrowed", "sa:t")],
        {
            ("busy", "sa:t"): now - timedelta(seconds=10),
            ("quiet", "sa:t"): now - timedelta(hours=1),
            ("borrowed", "sa:eve"): now - timedelta(seconds=10),  # another token's label
        },
    )
    monkeypatch.setattr(run_handlers, "get_client", lambda: ledger)
    assert await run_handlers.close_idle_session_runs(IDLE) == 2
    assert [c.run_id for c in completes] == ["quiet", "borrowed"]
    assert {c.metadata["closed_by"] for c in completes} == {"mcp_idle"}
    assert {(c.status, c.outcome) for c in completes} == {("completed", "none")}


@pytest.mark.asyncio
async def test_a_run_whose_close_fails_does_not_hold_up_the_rest(monkeypatch, completes) -> None:
    ledger = _Ledger([_row("broken", ""), _row("next", "")], {})
    monkeypatch.setattr(run_handlers, "get_client", lambda: ledger)
    assert await run_handlers.close_idle_session_runs(IDLE) == 1
    assert [c.run_id for c in completes] == ["next"]


@pytest.mark.asyncio
async def test_no_stale_run_reads_no_calls(monkeypatch, completes) -> None:
    ledger = _Ledger([], {})
    monkeypatch.setattr(run_handlers, "get_client", lambda: ledger)
    assert await run_handlers.close_idle_session_runs(IDLE) == 0
    assert ledger.tables == ["agent_runs"]


@pytest.mark.asyncio
async def test_the_worker_pass_closes_with_the_idle_window(monkeypatch) -> None:
    """The `mcp_session_run_sweep` worker's pass reads the window from settings."""
    from server.settings import settings
    from server.tasks.workers import session_run_sweep

    windows: list[float] = []

    async def close(idle_seconds: float) -> int:
        windows.append(idle_seconds)
        return 2

    monkeypatch.setattr(settings, "mcp_session_run_idle_seconds", 900)
    monkeypatch.setattr(run_handlers, "close_idle_session_runs", close)
    assert await session_run_sweep.run_pass() == 2
    assert windows == [900]
