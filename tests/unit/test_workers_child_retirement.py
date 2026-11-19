"""The reaped worker child's original process identity gates retirement."""

import asyncio
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.core import provider_process
from server.insights.app import provider_barrier as barrier
from server.insights.store import provider_participants as store
from server.settings import Settings, settings
from server.workers import child
from tests.unit.clock_fake import ManualClock


@pytest.mark.asyncio
async def test_actual_reap_certifies_its_exact_uncertified_child(monkeypatch):
    workers, proc, proof, record, publish = await _owned(monkeypatch)
    try:
        await workers._reap(time.monotonic() + 10)
        assert proc.returncode == -signal.SIGTERM and proc.waits >= 1
        assert record.await_count == 1
        evidence = record.await_args.args[0]
        assert evidence.participant == UUID(int=17)
        assert evidence.host_proof == proof and evidence.after == 9
        barrier.running_queries.assert_awaited_once_with(UUID(int=17), ch=barrier._default_client())
        assert proc.signals == [signal.SIGTERM]
    finally:
        workers._close_pipe()
        publish.cancel()
        await asyncio.gather(publish, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_reap_does_not_rewrite_an_already_certified_child(monkeypatch):
    workers, proc, _, record, publish = await _owned(monkeypatch, fault="already_retired")
    try:
        await workers._reap(time.monotonic() + 10)
        assert proc.returncode == -signal.SIGTERM
        assert not record.await_count
    finally:
        workers._close_pipe()
        publish.cancel()
        await asyncio.gather(publish, return_exceptions=True)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "wrong_pid",
        "reused_pid_proof",
        "different_namespace",
        "own_uuid",
        "alive",
        "queries_running",
        "queries_unknown",
        "query_error",
        "birth_unknown",
        "birth_other_parent",
        "birth_wrong_namespace",
        "parent_changed",
    ],
)
@pytest.mark.asyncio
async def test_reap_retirement_refuses_unknown_or_unsettled_identity(monkeypatch, fault):
    workers, proc, _, record, publish = await _owned(monkeypatch, fault=fault)
    try:
        await workers._reap(time.monotonic() + 10)
        assert proc.returncode == -signal.SIGTERM
        assert not record.await_count
    finally:
        workers._close_pipe()
        publish.cancel()
        await asyncio.gather(publish, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_live_owned_child_cannot_be_certified_before_its_wait(monkeypatch):
    workers, proc, _, record, publish = await _owned(monkeypatch)
    try:
        await workers._retire_child(proc, time.monotonic() + 10)
        assert proc.returncode is None and proc.waits == 0
        assert not record.await_count
    finally:
        workers._close_pipe()
        publish.cancel()
        await asyncio.gather(publish, return_exceptions=True)


@pytest.mark.asyncio
async def test_exhausted_original_stop_budget_refuses_retirement(monkeypatch):
    workers, proc, _, record, publish = await _owned(monkeypatch)
    try:
        await workers._reap(time.monotonic() - 1)
        assert proc.returncode is not None
        assert not record.await_count
    finally:
        workers._close_pipe()
        publish.cancel()
        await asyncio.gather(publish, return_exceptions=True)


@pytest.mark.asyncio
async def test_exact_proof_participant_read_is_bounded_and_parameterized(monkeypatch):
    client = SimpleNamespace(query_all=AsyncMock(return_value=[]))
    proof = "host|boot|container|77|123|901|||cell-retirement-v1"
    assert await store.participants(ch=client, host_proof=proof) == []
    sql, params = client.query_all.await_args.args
    assert sql == store.PARTICIPANTS_SQL + " AND host_proof = {proof:String} LIMIT 2"
    assert params == {"proof": proof}
    assert proof not in sql


async def _owned(monkeypatch, *, fault=None):
    parent = os.getpid()
    pid = parent + 100000
    here = f"host|boot|container|77|{parent}|900||"
    proof = f"host|boot|container|77|{pid}|901|||cell-retirement-v1"
    rows = [{"participant_id": UUID(int=17), "host_proof": proof, "state": "drained", "version": 9}]
    if fault in {"missing", "already_retired"}:
        rows = []
    elif fault == "duplicate":
        rows *= 2
    elif fault in {"wrong_pid", "reused_pid_proof", "different_namespace"}:
        parts = proof.split("|")
        parts[{"wrong_pid": 4, "reused_pid_proof": 5, "different_namespace": 3}[fault]] = "999"
        rows[0]["host_proof"] = "|".join(parts)
    elif fault == "own_uuid":
        rows[0]["participant_id"] = UUID(int=18)

    async def authority(sql, params=None):
        from server.core.pg_profile import _CH_RECORD

        assert sql == _CH_RECORD
        return {"authority": "clickhouse", "deployment_id": str(UUID(int=1)), "epoch": 1}

    client = SimpleNamespace(query_one=authority)
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    monkeypatch.setattr(barrier, "_default_client", lambda: client)
    participants = AsyncMock(return_value=rows)
    monkeypatch.setattr(store, "participants", participants)
    monkeypatch.setattr(barrier, "participants", participants)
    monkeypatch.setattr(barrier, "process_identity", lambda: UUID(int=18))
    monkeypatch.setattr(barrier, "proves_stopped", lambda _: fault != "alive")
    monkeypatch.setattr(provider_process, "proves_stopped", lambda _: fault != "alive")
    monkeypatch.setattr(provider_process, "host_proof", lambda: here)
    queries = AsyncMock(
        return_value=None if fault == "queries_unknown" else int(fault == "queries_running")
    )
    if fault == "query_error":
        queries.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(barrier, "running_queries", queries)
    record = AsyncMock()
    monkeypatch.setattr(barrier, "record_retirement", record)
    original_read, original_stat = Path.read_text, os.stat
    fields = ["S", str(parent)] + ["0"] * 18
    fields[19] = "901"
    if fault == "birth_other_parent":
        fields[1] = str(parent + 1)

    def read(path, *args, **kwargs):
        if path == Path(f"/proc/{pid}/stat"):
            if fault == "birth_unknown":
                raise PermissionError("uninspectable")
            return f"{pid} (python) " + " ".join(fields)
        return original_read(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        if str(path) == f"/proc/{pid}/ns/pid":
            return SimpleNamespace(st_ino=78 if fault == "birth_wrong_namespace" else 77)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(os, "stat", stat)

    class Process:
        returncode = None
        waits = 0

        def __init__(self):
            self.pid = pid
            self.signals = []

        def send_signal(self, sig):
            self.signals.append(sig)

        def kill(self):
            self.returncode = -signal.SIGKILL

        async def wait(self):
            self.waits += 1
            self.returncode = -signal.SIGTERM if self.returncode is None else self.returncode
            return self.returncode

    proc = Process()
    monkeypatch.setattr(child.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    lease = SimpleNamespace(fresh=lambda: True, remaining_s=lambda: 10)
    workers = child.ChildWorkers(lease, retire_child=barrier.retire_reaped_process)
    await workers._spawn()
    if fault == "parent_changed":
        monkeypatch.setattr(
            provider_process, "host_proof", lambda: f"host|boot|container|77|{parent + 1}|902||"
        )
    return workers, proc, proof, record, workers._publish


@pytest.mark.asyncio
async def test_naturally_exited_owned_child_is_retired_within_restart_delay(monkeypatch):
    workers, _, proof, record, publish = await _owned(monkeypatch)
    clock = [time.monotonic()]
    sleeps = []

    class StopLoop(Exception):
        pass

    async def record_with_elapsed_proof(_, *, ch=None):
        clock[0] += 0.4

    async def stop_after_restart_delay(delay):
        sleeps.append(delay)
        raise StopLoop

    record.side_effect = record_with_elapsed_proof
    monkeypatch.setattr(workers, "_spawn", AsyncMock())
    monkeypatch.setattr(child, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(child.asyncio, "sleep", stop_after_restart_delay)
    try:
        with pytest.raises(StopLoop):
            await workers._keep_running()
        assert record.await_count == 1
        assert record.await_args.args[0].host_proof == proof
        assert sleeps == [pytest.approx(child.RESTART_DELAY_S - 0.4)]
        assert workers.restarts == 1
    finally:
        workers._close_pipe()
        publish.cancel()
        await asyncio.gather(publish, return_exceptions=True)


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_child_retirement_proof_timeout_requires_a_positive_finite_setting(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, workers_child_retirement_timeout_s=value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "remaining", "expected"),
    [(0.125, 2.0, 0.125), (1.0, 0.25, 0.25), (2.0, 0.0625, 0.0625)],
)
async def test_child_retirement_setting_caps_only_the_original_remaining_allowance(
    monkeypatch, configured, remaining, expected
):
    workers, proc, _, record, publish = await _owned(monkeypatch)
    clock = ManualClock(t=100.0)
    deadlines = []
    callback_ends = []
    original_timeout = asyncio.timeout
    original_callback = workers._retire_callback

    def observed_timeout_at(end):
        deadlines.append(end)
        return original_timeout(max(0.0, end - clock.monotonic()))

    async def observed_callback(proof, end):
        callback_ends.append(end)
        await original_callback(proof, end)

    monkeypatch.setattr(child.settings, "workers_child_retirement_timeout_s", configured)
    monkeypatch.setattr(child, "time", clock)
    monkeypatch.setattr(child.asyncio, "timeout_at", observed_timeout_at)
    monkeypatch.setattr(workers, "_retire_callback", observed_callback)
    try:
        await workers._reap(clock.monotonic() + remaining)
        assert proc.returncode == -signal.SIGTERM and proc.waits >= 1
        assert record.await_count == 1
        assert callback_ends == [100.0 + expected]
        assert deadlines and set(deadlines) == {100.0 + expected}
        assert clock.monotonic() == 100.0
    finally:
        workers._close_pipe()
        publish.cancel()
        await asyncio.gather(publish, return_exceptions=True)
