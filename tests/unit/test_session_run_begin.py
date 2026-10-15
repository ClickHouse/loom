"""`begin_session_run`: what a session run's begin writes. A re-open keeps the
keys the idle sweep reads however many keys an earlier close recalled, and a
first begin is versioned by its start."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from server.tasks.api.models_run import RunBeginInput
from server.tasks.app import run as run_handlers

SESSION_META = {"implicit": True, "transport": "mcp", "subject": "human:ada"}


class _Ledger:
    def __init__(self, existing: dict[str, Any] | None) -> None:
        self.existing = existing
        self.inserted: list[dict[str, Any]] = []

    async def fetch(self, ch: Any, org: str, ns: str, run_id: str) -> dict[str, Any] | None:
        return self.existing

    async def insert(self, ch: Any, rows: list[dict[str, Any]]) -> None:
        self.inserted.extend(rows)


async def _begin(
    monkeypatch: pytest.MonkeyPatch, existing: dict[str, Any] | None, started_at: datetime
) -> dict[str, Any]:
    ledger = _Ledger(existing)
    monkeypatch.setattr(run_handlers, "get_client", lambda: None)
    monkeypatch.setattr(run_handlers.runs_store, "fetch_run", ledger.fetch)
    monkeypatch.setattr(run_handlers.runs_store, "insert_runs", ledger.insert)
    inp = RunBeginInput(
        org="o",
        namespace="n",
        agent="a",
        goal="mcp session (a)",
        run_id="mcp-" + "0" * 40,
        metadata=SESSION_META,
    )
    await run_handlers.begin_session_run(inp, started_at=started_at)
    (row,) = ledger.inserted
    return row


@pytest.mark.asyncio
async def test_a_reopen_past_the_metadata_limit_keeps_the_session_keys(monkeypatch) -> None:
    recalled = [f"project/fact/{i:04d}-{'x' * 40}" for i in range(500)]
    closed = {**SESSION_META, "recalled_keys": recalled, "closed_by": "mcp_idle"}
    existing = {"status": "completed", "metadata": json.dumps(closed), "started_at": None}
    row = await _begin(monkeypatch, existing, datetime(2026, 10, 1))
    meta = json.loads(row["metadata"])
    assert len(row["metadata"]) <= run_handlers._METADATA_LIMIT
    assert {k: meta[k] for k in SESSION_META} == SESSION_META
    assert 0 < len(meta["recalled_keys"]) < len(recalled)
    assert meta["recalled_keys"] == recalled[: len(meta["recalled_keys"])]
    assert "closed_by" not in meta


@pytest.mark.asyncio
async def test_a_first_begin_is_versioned_by_its_start(monkeypatch) -> None:
    started = datetime(2026, 10, 1)
    row = await _begin(monkeypatch, None, started)
    started_ns = int((started - datetime(1970, 1, 1)).total_seconds()) * 10**9
    assert row["version"] == run_handlers._FIRST_BEGIN_VERSION_BASE - started_ns
