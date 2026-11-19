"""Generation cleanup adds no suspension unless an old client must close."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core.vectors import admission
from server.settings import settings
from tests.unit.test_vector_read_admission import Driver, client


@pytest.mark.parametrize("vector", [False, True], ids=["ordinary", "vector"])
@pytest.mark.parametrize("retired_busy", [False, True], ids=["current", "retired-still-used"])
async def test_completed_query_does_not_yield_for_noop_generation_retirement(
    monkeypatch: pytest.MonkeyPatch, vector: bool, retired_busy: bool
) -> None:
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    ch, driver = client(monkeypatch, pool=2)
    driver.release.set()
    replacement = Driver()
    replacement.release.set()
    closed: list[bool] = []

    async def connect() -> None:
        ch._client = replacement

    async def close() -> None:
        closed.append(True)

    monkeypatch.setattr(ch, "connect", connect)
    monkeypatch.setattr(driver, "close", close, raising=False)
    query = driver.query

    async def complete(sql: str, **kwargs: Any) -> Any:
        result = await query(sql, **kwargs)
        if retired_busy:
            await ch._reconnect(seen=0)
        return result

    monkeypatch.setattr(driver, "query", complete)
    if retired_busy:
        # One other owner still uses generation zero when this query reconnects.
        ch._inflight[0] = 1
    turns: list[str] = []
    scheduled = asyncio.get_running_loop().call_soon(turns.append, "next-turn")
    try:
        with admission.vector_work("one-org", "private", int(vector)):
            assert await ch.query_all("SELECT 1") == []
        assert turns == [], "a completed query must return before the next event-loop turn"
        assert driver.started == 1 and driver.active == 0
        assert gate.population == 0 and gate.orgs == {}
        assert ch._slots is not None and ch._slots._value == 2
        assert closed == []
        if retired_busy:
            assert ch.generation == 1
            assert ch._inflight == {0: 1}
            assert ch._retired == {0: driver}
        else:
            assert ch.generation == 0
            assert ch._inflight == {} and ch._retired == {}
        if vector:
            assert driver.settings[0]["read_overflow_mode"] == "throw"
            assert driver.settings[0]["max_rows_to_read"] == settings.vector_scan_max_rows_to_read
    finally:
        scheduled.cancel()
        if retired_busy:
            ch._inflight[0] = 0
            await ch._retire_if_done(0)
    assert ch._inflight == {} and ch._retired == {}
    assert closed == ([True] if retired_busy else [])
    assert admission.current_work() is None


@pytest.mark.parametrize(
    ("failed", "cancelled"),
    [(False, False), (False, True), (True, True)],
    ids=["success", "repeated-cancellation", "first-error-with-repeated-cancellation"],
)
async def test_real_generation_close_keeps_credit_until_owned_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch, failed: bool, cancelled: bool
) -> None:
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    ch, driver = client(monkeypatch, pool=2)
    driver.release.set()
    replacement = Driver()
    replacement.release.set()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    closed: list[bool] = []
    original = RuntimeError("driver failure before generation cleanup")
    query = driver.query

    async def connect() -> None:
        ch._client = replacement

    async def close() -> None:
        close_started.set()
        await release_close.wait()
        closed.append(True)

    async def reconnect_during_query(sql: str, **kwargs: Any) -> Any:
        result = await query(sql, **kwargs)
        await ch._reconnect(seen=0)
        if failed:
            raise original
        return result

    monkeypatch.setattr(ch, "connect", connect)
    monkeypatch.setattr(driver, "close", close, raising=False)
    monkeypatch.setattr(driver, "query", reconnect_during_query)

    async def read() -> Any:
        with admission.vector_work("one-org", "private"):
            return await ch.query_all("SELECT 1")

    caller = asyncio.create_task(read())
    try:
        async with asyncio.timeout(1):
            await close_started.wait()
        assert ch.generation == 1 and ch._client is replacement
        assert ch._inflight == {} and ch._retired == {}
        assert closed == [] and not caller.done()
        assert gate.population == 1 and gate.orgs["one-org"].held == 1
        assert ch._slots is not None and ch._slots._value == 1
        if cancelled:
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done() and closed == []
            assert gate.population == 1 and gate.orgs["one-org"].held == 1
            assert ch._slots._value == 1
    finally:
        release_close.set()
        results = await asyncio.gather(caller, return_exceptions=True)
    assert closed == [True]
    if failed:
        assert results[0] is original
    elif cancelled:
        assert isinstance(results[0], asyncio.CancelledError)
    else:
        assert results == [[]]
    assert driver.started == 1 and driver.active == 0
    assert gate.population == 0 and gate.orgs == {}
    assert ch._slots is not None and ch._slots._value == 2
    assert ch._inflight == {} and ch._retired == {}
    assert admission.current_work() is None
