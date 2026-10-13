"""Run.complete reads the run with sequential consistency,
and the run.begin ledger row is labelled with the run it opened."""

from __future__ import annotations

import pytest

from server.core.ch import sequential_reads_active
from server.core.errors import NotFoundError
from server.observability.run_context import current_ledger_run, set_ledger_run
from server.tasks.api.models_run import RunBeginInput, RunCompleteInput
from server.tasks.app import run as run_handlers


class _Client:
    """A client whose only visible behaviour is the consistency of its reads."""

    def __init__(self) -> None:
        self.sequential_reads: list[bool] = []

    async def query_one(self, sql: str, params=None):
        self.sequential_reads.append(sequential_reads_active())
        return None

    async def insert(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_complete_reads_the_run_with_sequential_consistency(monkeypatch):
    client = _Client()
    monkeypatch.setattr(run_handlers, "get_client", lambda: client)
    with pytest.raises(NotFoundError):
        await run_handlers.handle_run_complete(
            RunCompleteInput(org="o", namespace="n", run_id="r1", status="completed")
        )
    assert client.sequential_reads == [True], (
        "the point read that decides NOT_FOUND must see the begin's synchronous insert"
    )
    assert sequential_reads_active() is False, "the consistency window closes with the read"


@pytest.mark.asyncio
async def test_begin_labels_its_ledger_row_with_the_run_id(monkeypatch):
    client = _Client()
    monkeypatch.setattr(run_handlers, "get_client", lambda: client)
    # No Keeper: the claim adds no read here.
    monkeypatch.setattr(run_handlers.runs_store, "_claims_atomic", False)
    set_ledger_run("")
    out = await run_handlers.handle_run_begin(
        RunBeginInput(org="o", namespace="n", agent="a", goal="triage", run_id="r1")
    )
    assert out.run_id == "r1"
    assert current_ledger_run() == out.run_id
    # A begin drops its Keeper claim once its row lands, so the existence check is
    # what keeps a later user off the id: it must see that insert. So must the read
    # of the caller's own fallback run, which a lost answer's retry re-opens.
    assert client.sequential_reads == [True, True]


@pytest.mark.asyncio
async def test_begin_with_a_client_run_id_labels_that_id(monkeypatch):
    client = _Client()
    monkeypatch.setattr(run_handlers, "get_client", lambda: client)
    set_ledger_run("ambient-header-run")
    out = await run_handlers.handle_run_begin(
        RunBeginInput(org="o", namespace="n", agent="a", goal="g", run_id="client-chosen")
    )
    assert out.run_id == "client-chosen"
    assert current_ledger_run() == "client-chosen"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "last_call_s_ago", "idle_s", "expected"),
    [
        (True, 600.0, 300.0, ("ns-first", 4)),  # nobody called within the window
        (True, 10.0, 300.0, None),  # a sibling web process still serves it
        (True, 10.0, None, ("ns-first", 4)),  # the client ended the session: no wait
        (False, 600.0, 300.0, None),  # a sibling already closed it
    ],
)
async def test_an_implicit_run_closes_on_what_the_ledger_says(
    monkeypatch, active, last_call_s_ago, idle_s, expected
):
    """Read org-wide: a session's calls may name other namespaces than its run's."""
    from datetime import timedelta

    from server.core.datetimes import naive_utc_now

    now = naive_utc_now()

    class _Ledger(_Client):
        async def query_one(self, sql: str, params=None):
            await super().query_one(sql, params)
            assert "namespace =" not in sql
            if "agent_events" in sql:
                assert params["since"] == now - timedelta(hours=1)
                return {"n": 4, "last": now - timedelta(seconds=last_call_s_ago)}
            if not active:
                return None
            return {
                "namespace": "ns-first",
                "status": "active",
                "started_at": now - timedelta(hours=1),
            }

    client = _Ledger()
    monkeypatch.setattr(run_handlers, "get_client", lambda: client)
    monkeypatch.setattr(run_handlers, "_now", lambda: now)
    got = await run_handlers.idle_session_run("o", "mcp-s", idle_s=idle_s)
    assert got == expected
    assert client.sequential_reads[0] is True
