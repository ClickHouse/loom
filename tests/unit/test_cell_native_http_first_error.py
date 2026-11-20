"""Native HTTP clients preserve the first outcome through required exit."""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from server.cli import cell_attach, cell_control, cell_prepare
from server.settings import settings

_CLIENT = httpx.AsyncClient
_BOUNDARIES = ("control", "health", "register")
FIRST_ERRORS = (
    ValueError,
    httpx.ReadError,
    TimeoutError,
    asyncio.CancelledError,
    SystemExit,
    KeyboardInterrupt,
)
EXIT_ERRORS = (
    RuntimeError,
    ValueError,
    httpx.ReadError,
    TimeoutError,
    asyncio.CancelledError,
    SystemExit,
    KeyboardInterrupt,
)


@pytest.fixture
def exchange(monkeypatch):
    state = SimpleNamespace(
        first=None,
        second=None,
        phase="request",
        close_phase="await",
        events=[],
        exit_args=None,
        entered=asyncio.Event(),
        block=False,
        answer={"org": "acme", "cell": "eu-1", "deployment_id": "dep-eu-1"},
        status=200,
        close_entered=asyncio.Event(),
        close_block=False,
    )

    async def handler(request):
        state.events.append("request")
        state.entered.set()
        if state.block:
            try:
                await asyncio.Future()
            except asyncio.CancelledError as error:
                state.first = error
                raise
        if state.first is not None:
            raise state.first
        return httpx.Response(state.status, json=state.answer)

    class Client(_CLIENT):
        async def __aenter__(self):
            state.events.append("enter")
            if state.phase == "enter":
                raise state.first
            return await super().__aenter__()

        def __aexit__(self, *args):
            state.exit_args = args
            state.events.append("exit-call")
            if state.close_phase == "construct" and state.second is not None:
                raise state.second

            async def close():
                state.events.append("exit-await")
                state.close_entered.set()
                if state.close_block:
                    await asyncio.Future()
                await super(Client, self).__aexit__(*args)
                if state.second is not None:
                    raise state.second

            return close()

    def client(**kwargs):
        state.events.append("construct")
        if state.phase == "construct":
            raise state.first
        kwargs["transport"] = httpx.MockTransport(handler)
        return Client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "router_operator_token", "inert-test-token")
    return state


async def invoke(boundary):
    url = httpx.URL("https://router.test")
    if boundary == "control":
        return await cell_control.request(url, "eu-1", None, "/claim", {})
    if boundary == "health":
        return await cell_attach.Router(url, "eu-1", None).health("eu-1.example.test")
    return await cell_prepare._register(argparse.Namespace(cell="eu-1", wait=0), url, {}, None)


async def observed(callback):
    # Native fatal values must stay in this task rather than reach the event loop.
    try:
        return "value", await callback()
    except BaseException as error:  # noqa: BLE001 — compare original native failure objects.
        return "error", error


def assert_first(boundary, state, outcome):
    kind, result = outcome
    if boundary == "health" and isinstance(state.first, (httpx.HTTPError, ValueError)):
        assert (kind, result) == ("value", "")
    elif (boundary == "control" and isinstance(state.first, (httpx.HTTPError, TimeoutError))) or (
        boundary == "register"
        and state.phase == "request"
        and isinstance(state.first, httpx.HTTPError)
    ):
        expected = cell_prepare.Refused if boundary == "register" else cell_control.Refused
        assert kind == "error" and isinstance(result, expected)
        assert type(state.first).__name__ in str(result)
    else:
        assert kind == "error" and result is state.first


def assert_exit_first(boundary, state):
    first = state.exit_args[1]
    if boundary == "register" and isinstance(state.first, httpx.HTTPError):
        assert isinstance(first, cell_prepare.Refused)
    else:
        assert first is state.first


def assert_close_failure(boundary, state, outcome):
    kind, result = outcome
    if boundary == "control" and isinstance(state.second, (httpx.HTTPError, TimeoutError)):
        assert kind == "error" and isinstance(result, cell_control.Refused)
    else:
        assert kind == "error" and result is state.second


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
    expected = "dep-eu-1" if boundary == "health" else exchange.answer
    assert result == expected
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


@pytest.mark.parametrize("second_kind", EXIT_ERRORS)
@pytest.mark.parametrize("close_phase", ("construct", "await"))
@pytest.mark.parametrize("semantic", ("malformed", "backend", "probe", "refusal"))
async def test_registration_selected_refusal_survives_exit(
    monkeypatch, exchange, second_kind, close_phase, semantic
):
    selected = cell_prepare.Refused("registration-first")
    monkeypatch.setattr(cell_prepare, "Refused", lambda message: selected)
    exchange.status = 200 if semantic == "malformed" else 503
    exchange.answer = (
        {"unexpected": True}
        if semantic == "malformed"
        else {
            "error": "registration-first",
            "code": {"backend": "BACKEND_UNAVAILABLE", "probe": "PROBE_BUSY", "refusal": "OTHER"}[
                semantic
            ],
        }
    )
    exchange.second = second_kind("exit-second")
    exchange.close_phase = close_phase
    kind, result = await observed(lambda: invoke("register"))
    assert kind == "error" and result is selected
    assert exchange.exit_args[1] is selected
