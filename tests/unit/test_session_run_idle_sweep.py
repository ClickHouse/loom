"""The idle sweep closes an MCP session's implicit run from the ledger alone.
The query keeps a run its own token called lately (live on ClickHouse in
tests/integration/test_session_run_ledger_live_ch.py); here: a pass pages
through the backlog, and one run whose close fails does not hold up the others."""

from __future__ import annotations

from typing import Any

import pytest

from server.tasks.app import run as run_handlers


class _Ledger:
    """Answers the idle query with the runs not completed yet, a page at a time."""

    def __init__(self, *run_ids: str) -> None:
        self.rows = [{"org": "o", "namespace": "n", "run_id": r} for r in run_ids]
        self.completes: list[Any] = []
        self.params: list[Any] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        done = {c.run_id for c in self.completes}
        active = [r for r in self.rows if r["run_id"] not in done]
        if "FINAL" not in sql:  # the candidates
            return [{"run_id": r["run_id"]} for r in active]
        assert "NOT IN" in sql and params["ids"] == [r["run_id"] for r in active]
        self.params.append({k: v for k, v in params.items() if k != "ids"})
        return active[: params["lim"]]

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
    assert ledger.params == [{"org": "o", "idle": 1800, "lim": 50}]
    (c,) = ledger.completes
    assert (c.org, c.namespace, c.run_id) == ("o", "n", "a")
    assert (c.status, c.outcome, c.metadata) == ("completed", "none", {"closed_by": "mcp_idle"})


@pytest.mark.asyncio
async def test_a_pass_pages_through_the_backlog(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, "a", "b", "c")
    assert await run_handlers.close_idle_session_runs(1800, page=2) == 3
    assert len(ledger.params) == 2  # a full page, then a short one


@pytest.mark.asyncio
async def test_a_run_whose_close_fails_does_not_hold_up_the_rest(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, "broken", "next")
    assert await run_handlers.close_idle_session_runs(1800) == 1
    assert [c.run_id for c in ledger.completes] == ["next"]


@pytest.mark.asyncio
async def test_a_page_of_runs_that_fail_to_close_ends_the_pass(monkeypatch) -> None:
    ledger = _ledger(monkeypatch, "broken-1", "broken-2", "after")
    assert await run_handlers.close_idle_session_runs(1800, page=2) == 0
    assert len(ledger.params) == 1  # no spinning on the same page until the deadline


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


@pytest.mark.asyncio
async def test_no_active_session_run_skips_the_final_read(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    assert await run_handlers.close_idle_session_runs(1800) == 0
    assert ledger.params == []
