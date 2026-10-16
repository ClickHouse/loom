"""A worker pass that was stopped, or whose supervisor lease went stale,
issues no further write: its loops call `pass_context.check_stop()` between
batches, in its threads too."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.index import scip_delta as sd
from server.workers import leader, pass_context
from server.workers.leader import SupervisorLease
from tests.unit.test_scip_apply_rounds import _Cfg, _delta, _RoundRepo


class _Lease(SupervisorLease):
    def __init__(self) -> None:
        super().__init__("auto")
        self.stale = False

    def fresh(self) -> bool:
        return not self.stale


@pytest.fixture
def lease(monkeypatch: pytest.MonkeyPatch) -> _Lease:
    current = _Lease()
    monkeypatch.setattr(leader, "_CURRENT", current)
    return current


def test_check_stop_outside_a_pass_does_nothing(lease: _Lease) -> None:
    lease.stale = True  # a request path: no pass, no lease of its own
    pass_context.clear()
    pass_context.check_stop()
    assert pass_context.stop_requested() is False


def test_check_stop_raises_once_stopped_or_stale(lease: _Lease) -> None:
    ctx = pass_context.begin("w")
    try:
        pass_context.check_stop()
        lease.stale = True
        with pytest.raises(pass_context.PassStopped):
            pass_context.check_stop()
        lease.stale = False
        ctx.stopped.set()
        with pytest.raises(pass_context.PassStopped):
            pass_context.check_stop()
    finally:
        pass_context.clear()
    # Not an Exception: a leg's `except Exception` does not carry on past it.
    assert not issubclass(pass_context.PassStopped, Exception)


async def test_a_thread_of_a_cancelled_pass_sees_the_stop(lease: _Lease) -> None:
    """`asyncio.to_thread` carries the pass context: thread work the cancel
    cannot reach stops at its next check."""
    started, go = threading.Event(), threading.Event()
    seen: list[str] = []

    def work() -> None:
        started.set()
        go.wait(5)
        try:
            pass_context.check_stop()
            seen.append("wrote")
        except pass_context.PassStopped:
            seen.append("stopped")

    async def a_pass() -> None:
        ctx = pass_context.begin("w")
        try:
            await asyncio.to_thread(work)
        finally:
            ctx.stopped.set()  # what the supervisor does when a pass ends
            pass_context.clear()

    task = asyncio.create_task(a_pass())
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    go.set()
    for _ in range(200):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == ["stopped"]


def test_a_delta_stops_between_rounds_when_the_lease_goes_stale(lease: _Lease) -> None:
    class _Repo(_RoundRepo):
        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            await super().insert_rows(table, rows)
            lease.stale = True  # a stall after the first round's writes

    repo = _Repo()

    async def run() -> None:
        pass_context.begin("code_scip_delta")
        try:
            await sd.apply_delta(repo, "o", "n", _delta(250))  # type: ignore[arg-type]
        finally:
            pass_context.clear()

    with (
        patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()),
        pytest.raises(pass_context.PassStopped),
    ):
        asyncio.run(run())
    # The first round's two inserts, nothing after; no ctor-call state either.
    assert [t for t, _ in repo.inserts] == ["code_symbols", "code_edges"]


def test_a_delta_stops_when_its_entry_lease_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import entry_lock as el
    from tests.unit.claims_fake import HeldLease

    lease = HeldLease("scip_entry:o/n")

    class _Repo(_RoundRepo):
        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            await super().insert_rows(table, rows)
            lease.lost = True

    repo = _Repo()

    async def run() -> None:
        token = el._current.set(lease)  # type: ignore[arg-type]
        try:
            await sd.apply_delta(repo, "o", "n", _delta(250))  # type: ignore[arg-type]
        finally:
            el._current.reset(token)

    with (
        patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()),
        pytest.raises(el.EntryLockLost),
    ):
        asyncio.run(run())
    # Lost after the first insert: the next write checks the lease and publishes nothing.
    assert [t for t, _ in repo.inserts] == ["code_symbols"]


def test_the_ingest_budget_check_is_where_a_stopped_pass_stops(lease: _Lease) -> None:
    from server.code.app.index.pass_budget import pass_budget

    budget = pass_budget()
    assert budget.exhausted() is False  # outside a pass: just the clock
    pass_context.begin("code_ingest")
    try:
        assert budget.exhausted() is False
        lease.stale = True
        with pytest.raises(pass_context.PassStopped):
            budget.exhausted()
    finally:
        pass_context.clear()


async def test_community_propagation_in_its_thread_stops_with_the_pass(
    lease: _Lease, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import entity_communities as ec

    inserts: list[str] = []

    class _CH:
        async def insert(self, table: str, rows: list[Any], settings: Any = None) -> None:
            inserts.append(table)

    async def _namespaces(_ch: Any, _cfg: Any) -> list[tuple[str, str]]:
        return [("o", "a"), ("o", "b")]

    async def _edges(_ch: Any, org: str, ns: str, _cfg: Any) -> list[dict[str, Any]]:
        return [{"entity_a": f"{ns}{i}", "entity_b": f"{ns}{i + 1}", "w": 1} for i in range(20)]

    real = ec.rows_for_namespace

    def slow_rows(*a: Any, **k: Any) -> Any:
        lease.stale = True  # the lease goes stale while the thread computes
        time.sleep(0.01)
        return real(*a, **k)

    monkeypatch.setattr(ec, "_namespaces_with_edges", _namespaces)
    monkeypatch.setattr(ec, "_edges", _edges)
    monkeypatch.setattr(ec, "rows_for_namespace", slow_rows)
    pass_context.begin("entity_communities")
    try:
        with pytest.raises(pass_context.PassStopped):
            await ec.run_entity_communities(ch=_CH())  # type: ignore[arg-type]
    finally:
        pass_context.clear()
    assert inserts == []


async def test_the_supervisor_records_a_stopped_pass_and_keeps_its_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers import status_store
    from server.workers import supervisor as sup
    from server.workers.registry import WorkerSpec
    from tests.unit.test_supervisor_lease import _no_stagger

    rows: list[dict[str, Any]] = []
    passes: list[int] = []

    async def _pass() -> None:
        passes.append(1)
        if len(passes) == 1:
            raise pass_context.PassStopped

    async def _drained() -> None:
        return None

    monkeypatch.setattr(status_store, "drain", _drained)
    monkeypatch.setattr(sup, "_begin_loop", _no_stagger)
    monkeypatch.setattr(sup, "_record_status_row", lambda name, **kw: rows.append(kw))
    monkeypatch.setattr(sup, "enabled_workers", lambda: [WorkerSpec("w", 0.01, _pass)])
    s = sup.WorkerSupervisor()
    s.start_all()
    for _ in range(300):
        if len(passes) >= 2:
            break
        await asyncio.sleep(0.01)
    await s.stop_all(drain_timeout_s=1)
    assert len(passes) >= 2  # the loop went on
    assert rows[0]["cancelled"] is True and rows[0]["streak"] == 0
