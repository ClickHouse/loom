"""Placement HTTP failures keep their outcome through client and Postgres cleanup."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from server.cli import cell_place
from tests.unit import test_cell_native_http_first_error as native
from tests.unit import test_cell_place as place_helpers

local = place_helpers.local
invoke_place = place_helpers.invoke_place
exchange = native.exchange
observed = native.observed
assert_first = native.assert_first
assert_exit_first = native.assert_exit_first
assert_close_failure = native.assert_close_failure
FIRST_ERRORS = native.FIRST_ERRORS
EXIT_ERRORS = native.EXIT_ERRORS
_BOUNDARIES = ("placement",)
_DIRECTORY = cell_place._directory


async def invoke(boundary):
    return await cell_place._directory(
        httpx.URL("https://router.test"), "eu-1", "acme", "dep-eu-1", {}
    )


@pytest.mark.parametrize("boundary", _BOUNDARIES)
@pytest.mark.parametrize("first_kind", FIRST_ERRORS)
@pytest.mark.parametrize("second_kind", (*EXIT_ERRORS, None))
@pytest.mark.parametrize("close_phase", ("construct", "await"))
async def test_request_first_outcome_survives_client_exit(
    exchange, boundary, first_kind, second_kind, close_phase
):
    exchange.first = first_kind("request-first")
    exchange.second = second_kind("exit-second") if second_kind is not None else None
    exchange.close_phase = close_phase
    assert_first(boundary, exchange, await observed(lambda: invoke(boundary)))
    assert exchange.events[:3] == ["construct", "enter", "request"]
    assert exchange.events.count("exit-call") == 1
    assert_exit_first(boundary, exchange)


@pytest.mark.parametrize("boundary", _BOUNDARIES)
@pytest.mark.parametrize("second_kind", EXIT_ERRORS)
@pytest.mark.parametrize("close_phase", ("construct", "await"))
async def test_required_client_exit_failure_cannot_report_success(
    exchange, boundary, second_kind, close_phase
):
    exchange.second = second_kind("required-exit")
    exchange.close_phase = close_phase
    assert_close_failure(boundary, exchange, await observed(lambda: invoke(boundary)))
    assert exchange.exit_args == (None, None, None)


@pytest.mark.parametrize("boundary", _BOUNDARIES)
async def test_successful_client_neighbor(exchange, boundary):
    kind, result = await observed(lambda: invoke(boundary))
    assert kind == "value"
    assert result is None
    assert exchange.events == ["construct", "enter", "request", "exit-call", "exit-await"]


@pytest.mark.parametrize("boundary", _BOUNDARIES)
@pytest.mark.parametrize("phase", ("construct", "enter"))
@pytest.mark.parametrize("first_kind", FIRST_ERRORS)
async def test_client_acquisition_keeps_its_first_outcome(exchange, boundary, phase, first_kind):
    exchange.phase = phase
    exchange.first = first_kind("acquire-first")
    assert_first(boundary, exchange, await observed(lambda: invoke(boundary)))
    assert "request" not in exchange.events and "exit-call" not in exchange.events


@pytest.mark.parametrize("boundary", _BOUNDARIES)
@pytest.mark.parametrize("second_kind", EXIT_ERRORS)
async def test_actual_task_cancellation_keeps_its_payload(exchange, boundary, second_kind):
    exchange.block = True
    exchange.second = second_kind("exit-second")
    task = asyncio.create_task(observed(lambda: invoke(boundary)))
    try:
        await asyncio.wait_for(exchange.entered.wait(), 1)
        task.cancel("operator-first-stop")
        kind, result = await task
        assert kind == "error" and result is exchange.first
        assert isinstance(result, asyncio.CancelledError)
        assert result.args == ("operator-first-stop",)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("boundary", _BOUNDARIES)
async def test_repeated_cancellation_during_http_exit_keeps_first_payload(exchange, boundary):
    exchange.block = True
    exchange.close_block = True
    task = asyncio.create_task(observed(lambda: invoke(boundary)))
    try:
        await asyncio.wait_for(exchange.entered.wait(), 1)
        task.cancel("first-stop")
        await asyncio.wait_for(exchange.close_entered.wait(), 1)
        task.cancel("second-stop")
        kind, result = await task
        assert kind == "error" and result is exchange.first
        assert isinstance(result, asyncio.CancelledError) and result.args == ("first-stop",)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("first_kind", FIRST_ERRORS)
@pytest.mark.parametrize("second_kind", EXIT_ERRORS)
async def test_owned_placement_keeps_http_first_failure_and_closes_pg_once(
    local, exchange, monkeypatch, first_kind, second_kind
):
    conn, _ = local
    monkeypatch.setattr(cell_place, "_directory", _DIRECTORY)
    monkeypatch.setattr(cell_place, "_bound_s", lambda: 0.5)
    exchange.first = first_kind("request-first")
    exchange.second = second_kind("exit-second")
    kind, result = await observed(invoke_place)
    assert kind == "error"
    if first_kind is asyncio.CancelledError:
        # A cancelled child Task reconstructs this exception when Owner reads it.
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert result is exchange.first
    assert conn.calls.count("close") == 1
    assert conn.closed and not conn.terminated and not conn.committed
    assert not any("INSERT" in call for call in conn.calls)


@pytest.mark.parametrize("second_kind", EXIT_ERRORS)
async def test_owned_placement_required_http_close_failure_prevents_insert(
    local, exchange, monkeypatch, second_kind
):
    conn, _ = local
    monkeypatch.setattr(cell_place, "_directory", _DIRECTORY)
    monkeypatch.setattr(cell_place, "_bound_s", lambda: 0.5)
    exchange.second = second_kind("required-exit")
    kind, result = await observed(invoke_place)
    assert kind == "error"
    if second_kind is asyncio.CancelledError:
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert result is exchange.second
    assert conn.calls.count("close") == 1
    assert conn.closed and not conn.terminated and not conn.committed
    assert not any("INSERT" in call for call in conn.calls)


async def test_owned_placement_successful_http_neighbor_commits(local, exchange, monkeypatch):
    conn, _ = local
    monkeypatch.setattr(cell_place, "_directory", _DIRECTORY)
    monkeypatch.setattr(cell_place, "_bound_s", lambda: 0.5)
    assert await invoke_place() == exchange.answer
    assert conn.calls.count("close") == 1
    assert conn.closed and conn.committed and not conn.terminated
