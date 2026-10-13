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
@pytest.mark.parametrize("active", [True, False])
async def test_a_session_runs_calls_are_read_org_wide(monkeypatch, active):
    """A session's calls may name other namespaces than its run's."""
    from datetime import timedelta

    from server.core.datetimes import naive_utc_now

    now = naive_utc_now()
    started, last = now - timedelta(hours=1), now - timedelta(seconds=10)

    class _Ledger(_Client):
        async def query_one(self, sql: str, params=None):
            await super().query_one(sql, params)
            assert "namespace =" not in sql and "agent_events" in sql
            assert params["since"] == started and params["subject"] == "sa:t"
            return {"n": 4, "last": last}

        async def query_all(self, sql: str, params=None):
            self.sequential_reads.append(sequential_reads_active())
            assert "namespace =" not in sql
            return [{"namespace": "ns-first", "started_at": started}] if active else []

    client = _Ledger()
    monkeypatch.setattr(run_handlers, "get_client", lambda: client)
    got = await run_handlers.session_run_calls("o", "mcp-s", "sa:t")
    assert got == ((["ns-first"], 4, last) if active else None)
    assert client.sequential_reads[0] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supplied", "kept"),
    [("mcp-" + "a" * 40, False), ("mcp-server-nightly-1", True), ("my-run-1", True)],
)
async def test_an_explicit_begin_never_takes_a_session_runs_id(monkeypatch, supplied, kept):
    """Only the exact shape of a session run's id is the transport's."""
    client = _Client()
    monkeypatch.setattr(run_handlers, "get_client", lambda: client)
    out = await run_handlers.handle_run_begin(
        RunBeginInput(org="o", namespace="n", agent="a", goal="g", run_id=supplied, user_id="eve")
    )
    assert (out.run_id == supplied) is kept
