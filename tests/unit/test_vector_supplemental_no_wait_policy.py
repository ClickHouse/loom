"""Completion-sensitive reads refuse waits without changing ordinary admission."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from server.core.errors import VectorAdmissionError
from server.core.vectors import admission, quantized_setting
from server.settings import settings
from tests.unit.test_vector_read_admission import client, settled


def _gate(monkeypatch: pytest.MonkeyPatch) -> admission._Admission:
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    monkeypatch.setattr(settings, "vector_scan_max_concurrent_per_org", 2)
    monkeypatch.setattr(settings, "vector_scan_max_waiters_per_org", 256)
    return gate


async def test_weighted_immediate_grant_refusal_other_org_and_reuse(monkeypatch) -> None:
    gate = _gate(monkeypatch)
    work = admission.VectorWork("one", ("private",), 2)
    other = admission.VectorWork("two", ("private",), 2)
    assert not admission._without_wait.get()
    with admission._without_admission_wait():
        first, _ = await gate.enter(work, 1)
        try:
            assert gate.population == 2 and first.held == 2
            # Clearing statement attribution cannot clear the owner's wait policy.
            with admission.vector_work("", (), 0):
                with admission._without_admission_wait():
                    with pytest.raises(VectorAdmissionError) as caught:
                        await gate.enter(work, 1)
            assert admission._without_wait.get()
            assert caught.value.extra == {"reason": "org_queue", "retry_after_s": 1}
            assert first.waiting == 0 and not first.queue and gate.population == 2
            second, _ = await gate.enter(other, 1)
            assert second.held == 2 and gate.population == 4
            gate.leave(other, second)
        finally:
            gate.leave(work, first)
        reused, _ = await gate.enter(work, 1)
        gate.leave(work, reused)
    assert not admission._without_wait.get()
    assert admission.current_work() is None
    assert gate.population == 0 and gate.orgs == {}


async def test_sensitive_work_does_not_jump_an_older_weighted_fifo_head(monkeypatch) -> None:
    gate = _gate(monkeypatch)
    one = admission.VectorWork("one", ("private",), 1)
    two = admission.VectorWork("one", ("private",), 2)
    held, _ = await gate.enter(one, 1)
    older = asyncio.create_task(gate.enter(two, 1))
    try:
        await asyncio.sleep(0)
        assert held.held == 1 and held.waiting == 2 and len(held.queue) == 1
        with admission._without_admission_wait():
            with pytest.raises(VectorAdmissionError) as caught:
                await gate.enter(one, 1)
        assert caught.value.extra["reason"] == "org_queue"
        assert held.held == 1 and held.waiting == 2 and len(held.queue) == 1
        assert gate.population == 3 and not older.done()
    finally:
        older.cancel()
        result = await asyncio.gather(older, return_exceptions=True)
        gate.leave(one, held)
    assert isinstance(result[0], asyncio.CancelledError)
    assert gate.population == 0 and gate.orgs == {}


async def test_inherited_policy_resets_before_ordinary_wait_and_cancellation(monkeypatch) -> None:
    gate = _gate(monkeypatch)
    full = admission.VectorWork("one", ("private",), 2)
    one = admission.VectorWork("one", ("private",), 1)
    held, _ = await gate.enter(full, 1)
    tasks: list[asyncio.Task[Any]] = []
    try:
        with admission._without_admission_wait():
            sensitive = asyncio.create_task(gate.enter(one, 1))
            tasks.append(sensitive)
            with pytest.raises(VectorAdmissionError):
                await sensitive
        assert not admission._without_wait.get()
        ordinary = asyncio.create_task(gate.enter(one, 1))
        tasks.append(ordinary)
        await asyncio.sleep(0)
        assert not ordinary.done()
        assert held.waiting == 1 and gate.population == 3
        ordinary.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ordinary
        assert held.waiting == 0 and gate.population == 2
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        gate.leave(full, held)
    assert not admission._without_wait.get()
    assert gate.population == 0 and gate.orgs == {}


@pytest.mark.parametrize("quantized_retry", [False, True], ids=["plain", "quantized-retry"])
async def test_public_query_holds_nonwaiting_charge_through_pool_wait_and_retry(
    monkeypatch: pytest.MonkeyPatch, quantized_retry: bool
) -> None:
    from types import SimpleNamespace

    gate = _gate(monkeypatch)
    ch, driver = client(monkeypatch, pool=1)
    calls: list[tuple[str, dict[str, Any]]] = []
    by_owner: dict[int, list[str]] = {}
    state = quantized_setting.quantized_codes_state()
    quantized_setting.set_quantized_codes_supported(True)
    first = asyncio.Event()
    release = asyncio.Event()

    async def query(sql: str, **kwargs: Any) -> Any:
        calls.append((sql, kwargs["settings"]))
        owner = kwargs["parameters"]["owner"]
        attempts = by_owner.setdefault(owner, [])
        attempts.append(sql)
        first.set()
        await release.wait()
        if quantized_retry and owner == 1 and len(attempts) == 1:
            raise DatabaseError("Code: 115. Unknown setting vector_search_use_quantized_codes")
        return SimpleNamespace(result_rows=[(1,)], column_names=["value"], summary={})

    monkeypatch.setattr(driver, "query", query)
    plain = "SELECT {owner:UInt8} * 0 + 1 AS value SETTINGS max_threads=1"
    sql = plain
    if quantized_retry:
        sql += ", vector_search_use_quantized_codes = 1"

    async def read(owner: int) -> list[dict[str, Any]]:
        with admission._without_admission_wait(), admission.vector_work("one", "private"):
            return await ch.query_all(sql, {"owner": owner})

    tasks = [asyncio.create_task(read(1))]
    try:
        async with asyncio.timeout(1):
            await first.wait()
        tasks.append(asyncio.create_task(read(2)))
        await settled()
        assert len(calls) == 1
        assert gate.population == 2 and gate.orgs["one"].held == 2
        assert gate.orgs["one"].waiting == 0 and not gate.orgs["one"].queue
        assert ch._slots is not None and ch._slots._value == 0
        with pytest.raises(VectorAdmissionError) as caught:
            await read(3)
        assert caught.value.extra["reason"] == "org_queue"
        assert gate.population == 2 and len(calls) == 1
        release.set()
        assert await asyncio.gather(*tasks) == [[{"value": 1}], [{"value": 1}]]
        assert len(calls) == (3 if quantized_retry else 2)
        assert by_owner == {1: [sql, plain] if quantized_retry else [sql], 2: [sql]}
        if quantized_retry:
            assert not quantized_setting.quantized_codes_supported()
        for _, caps in calls:
            assert caps["max_rows_to_read"] == settings.vector_scan_max_rows_to_read
            assert caps["read_overflow_mode"] == "throw"
            assert 0 < caps["max_execution_time"] <= settings.ch_caller_shaped_max_execution_s
            assert 0 < caps["max_memory_usage"] <= settings.ch_caller_shaped_max_memory_bytes
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        quantized_setting.set_quantized_codes_supported(state)
    assert all(task.done() for task in tasks)
    assert ch._slots is not None and ch._slots._value == 1
    assert ch._inflight == {} and ch._retired == {}
    assert gate.population == 0 and gate.orgs == {}
    assert not admission._without_wait.get() and admission.current_work() is None


async def test_policy_scope_resets_when_its_owner_is_cancelled() -> None:
    started = asyncio.Event()
    released = asyncio.Event()
    restored: list[bool] = []

    async def owner() -> None:
        try:
            with admission._without_admission_wait():
                assert admission._without_wait.get()
                started.set()
                await released.wait()
        finally:
            restored.append(admission._without_wait.get())

    task = asyncio.create_task(owner())
    try:
        async with asyncio.timeout(1):
            await started.wait()
        assert not admission._without_wait.get()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        released.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert restored == [False]
    assert not admission._without_wait.get()
