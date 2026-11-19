"""Cancelled waiters cannot forget a replacement org's live native reads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.core import ch as ch_module
from server.core.vectors import admission
from server.settings import settings

ORG, NS = "same-org", "private"
BODY = "The complete first row.\nIts second line is retained."


def rows(label):
    return [
        {"batch": label, "body": BODY, "score": 0.25},
        {"batch": label, "body": BODY + "\nAnother complete row.", "score": 0.75},
    ]


class NativeDriver:
    def __init__(self):
        self.release = {label: asyncio.Event() for label in ("B", "C", "D", "E")}
        self.started = {label: asyncio.Event() for label in ("B", "C", "D", "E")}
        self.order = []
        self.active = 0
        self.peak = 0
        self.work = []
        self.native_settings = []
        self.org_at_dispatch = {}

    async def query(self, sql, *, parameters, settings, **kwargs):
        label = parameters["owner"]
        work = admission.current_work()
        assert work is not None and (work.org, work.namespaces) == (ORG, (NS,))
        assert parameters == {"owner": label, "org": ORG, "ns": NS, "q": [1.0, 0.0]}
        assert sql.count("cosineDistance") == work.weight
        assert "org = {org:String}" in sql and "namespace = {ns:String}" in sql
        assert settings["read_overflow_mode"] == "throw"
        assert settings["max_rows_to_read"] == 1_000_000
        assert 0 < settings["max_execution_time"] <= 30
        assert 0 < settings["max_memory_usage"] <= 4_000_000_000
        self.work.append(work)
        self.native_settings.append(dict(settings))
        self.org_at_dispatch[label] = admission._admission.orgs[ORG]
        self.order.append(label)
        self.active += work.weight
        self.peak = max(self.peak, self.active)
        self.started[label].set()
        try:
            await self.release[label].wait()
            result = rows(label)
            return SimpleNamespace(
                column_names=["batch", "body", "score"],
                result_rows=[tuple(r.values()) for r in result],
                summary={},
            )
        finally:
            self.active -= work.weight


@pytest.fixture
def owned_client(monkeypatch):
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    monkeypatch.setattr(settings, "vector_scan_max_concurrent_per_org", 2)
    monkeypatch.setattr(settings, "vector_scan_max_waiters_per_org", 8)
    monkeypatch.setattr(settings, "vector_scan_max_rows_to_read", 1_000_000)
    monkeypatch.setattr(settings, "ch_caller_shaped_max_execution_s", 30)
    monkeypatch.setattr(settings, "ch_caller_shaped_max_memory_bytes", 4_000_000_000)
    monkeypatch.setattr(settings, "ch_pool_maxsize", 8)
    monkeypatch.setattr(settings, "ch_pool_wait_ms", 5000)

    async def schema_ready(*args, **kwargs):
        return None

    monkeypatch.setattr(ch_module, "guard_pending_schema", schema_ready)
    client, driver = ch_module.CHClient(), NativeDriver()
    monkeypatch.setattr(client, "_client", driver)

    async def read(label, weight):
        probes = " + ".join(
            f"cosineDistance(embedding_{axis}, {{q:Array(Float32)}})" for axis in range(weight)
        )
        sql = f"SELECT batch, body, {probes} AS score FROM fixture_vectors WHERE org = {{org:String}} AND namespace = {{ns:String}} AND batch = {{owner:String}}"
        with admission.vector_work(ORG, NS, weight):
            return await client.query_all(
                sql, {"owner": label, "org": ORG, "ns": NS, "q": [1.0, 0.0]}
            )

    return SimpleNamespace(gate=gate, client=client, driver=driver, read=read)


async def cleanup(state, tasks):
    for release in state.driver.release.values():
        release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
    assert state.driver.active == 0
    assert state.gate.population == 0 and state.gate.orgs == {}
    assert state.client._slots is not None and state.client._slots._value == 8
    assert state.client._inflight == {} and state.client._retired == {}
    assert admission.current_work() is None


async def test_cancelled_waiter_cleanup_cannot_erase_replacement_org_under_real_client(
    owned_client,
):
    state = owned_client
    gate, driver = state.gate, state.driver
    full = admission.VectorWork(ORG, (NS,), 2)
    old, _ = await gate.enter(full, 5)
    left = False
    tasks = []
    try:
        cancelled = asyncio.create_task(state.read("B", 1))
        tasks.append(cancelled)
        await asyncio.sleep(0)
        assert old.held == 2 and old.waiting == 1 and len(old.queue) == 1
        assert gate.population == 3 and driver.order == []
        cancelled.cancel()
        assert old.queue[0].ready.cancelled() and not cancelled.done()
        # No suspension between cancellation and releasing the actual old lease.
        # leave removes its cancelled head and forgets only this empty generation.
        gate.leave(full, old)
        left = True
        assert ORG not in gate.orgs and gate.population == 1

        async def later_read():
            # This task is scheduled after the cancelled waiter's cleanup, and a
            # direct await of its completed task adds no scheduling turn.
            try:
                await cancelled
            except asyncio.CancelledError:
                pass
            return await state.read("D", 2)

        later = asyncio.create_task(later_read())
        tasks.append(later)

        async def observe():
            try:
                replacement = driver.org_at_dispatch["C"]
                current = gate.orgs[ORG]
                snapshot = {
                    "native_active_weight": driver.active,
                    "native_order": list(driver.order),
                    "same_replacement_generation": current is replacement,
                    "held": current.held,
                    "waiting": current.waiting,
                    "population": gate.population,
                }
                assert snapshot == {
                    "native_active_weight": 2,
                    "native_order": ["C"],
                    "same_replacement_generation": True,
                    "held": 2,
                    "waiting": 2,
                    "population": 4,
                }, (
                    "cancelled old waiter erased replacement ownership and exceeded the real C=2 native cap"
                )
                assert cancelled.cancelled() and replacement is not old
            finally:
                driver.release["C"].set()
                driver.release["D"].set()

        observer = asyncio.create_task(observe())
        tasks.append(observer)
        # This call runs inline until the driver's Event: C is admitted and live
        # before the event loop resumes B's already-scheduled cancellation cleanup.
        first_body = await state.read("C", 2)
        second_body = await later
        assert first_body == rows("C") and second_body == rows("D")
        assert gate.population == 0 and gate.orgs == {}
        driver.release["E"].set()
        assert await state.read("E", 2) == rows("E")
        assert driver.order == ["C", "D", "E"]
        assert [work.weight for work in driver.work] == [2, 2, 2]
        await observer
        assert driver.peak == 2
    finally:
        if not left:
            gate.leave(full, old)
        await cleanup(state, tasks)


async def test_cancelled_waiter_without_replacement_keeps_its_current_owner(owned_client):
    state = owned_client
    gate, driver = state.gate, state.driver
    full = admission.VectorWork(ORG, (NS,), 2)
    old, _ = await gate.enter(full, 5)
    left = False
    tasks = []
    try:
        cancelled = asyncio.create_task(state.read("B", 1))
        tasks.append(cancelled)
        await asyncio.sleep(0)
        assert old.waiting == 1 and gate.population == 3
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert gate.orgs[ORG] is old and old.held == 2
        assert old.waiting == 0 and not old.queue and gate.population == 2
        assert driver.order == []
        gate.leave(full, old)
        left = True
        assert gate.population == 0 and gate.orgs == {}
        driver.release["C"].set()
        assert await state.read("C", 2) == rows("C")
        assert driver.order == ["C"] and driver.peak == 2
    finally:
        if not left:
            gate.leave(full, old)
        await cleanup(state, tasks)


async def test_ordinary_weighted_fifo_preserves_complete_native_bodies(owned_client):
    state = owned_client
    gate, driver = state.gate, state.driver
    full = admission.VectorWork(ORG, (NS,), 2)
    old, _ = await gate.enter(full, 5)
    left = False
    tasks = []
    try:
        first = asyncio.create_task(state.read("B", 1))
        second = asyncio.create_task(state.read("C", 1))
        tasks.extend((first, second))
        await asyncio.sleep(0)
        assert old.waiting == 2 and [w.weight for w in old.queue] == [1, 1]
        assert gate.population == 4 and driver.order == []
        gate.leave(full, old)
        left = True
        later = asyncio.create_task(state.read("D", 2))
        tasks.append(later)
        await asyncio.sleep(0)
        assert driver.order == ["B", "C"] and driver.active == 2
        assert gate.orgs[ORG] is old and old.held == 2 and old.waiting == 2
        assert gate.population == 4
        driver.release["B"].set()
        assert await first == rows("B")
        assert driver.order == ["B", "C"] and driver.active == 1
        assert old.held == 1 and old.waiting == 2 and not later.done()
        driver.release["C"].set()
        assert await second == rows("C")
        await asyncio.wait_for(driver.started["D"].wait(), 1)
        assert gate.orgs[ORG] is old and old.held == 2 and old.waiting == 0
        driver.release["D"].set()
        assert await later == rows("D")
        assert driver.order == ["B", "C", "D"] and driver.peak == 2
        assert [work.weight for work in driver.work] == [1, 1, 2]
    finally:
        if not left:
            gate.leave(full, old)
        await cleanup(state, tasks)
