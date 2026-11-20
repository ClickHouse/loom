"""A complete HTTP exchange cannot outlive its directory-derived owner phase."""

from __future__ import annotations

import asyncio
import json
import time

import httpcore
import httpx
import pytest
import pytest_asyncio
from httpcore._backends.mock import AsyncMockBackend, AsyncMockStream

from server.cli import cell_control, cell_place
from server.settings import settings
from tests.unit import test_cell_place as helpers
from tests.unit.clock_fake import ManualClock

local = helpers.local
invoke_place = helpers.invoke_place

_DIRECTORY = cell_place._directory
pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest_asyncio.fixture(loop_scope="function")
async def exchange(local, monkeypatch):
    conn, _ = local
    before = set(asyncio.all_tasks())
    # All exchange timers share simulated time; CPU scheduling spends no HTTP budget.
    loop = asyncio.get_running_loop()
    phase_clock = ManualClock(t=loop.time())
    selector = loop._selector
    real_select = selector.select

    def select(timeout=None):
        if timeout is not None:
            phase_clock.advance(max(0.0, timeout))
            timeout = 0
        return real_select(timeout)

    monkeypatch.setattr(loop, "time", phase_clock.monotonic)
    monkeypatch.setattr(selector, "select", select)
    monkeypatch.setattr(cell_control, "time", phase_clock)
    monkeypatch.setattr(cell_control.pg, "time", phase_clock)
    events = []
    entered = asyncio.Event()
    state = {"mode": "response", "reads": [], "owners": []}
    real_close = conn.close
    real_call = cell_control.Owner.call
    actual_client = httpx.AsyncClient

    async def close(**kwargs):
        events.append("pg-close")
        await asyncio.sleep(0)
        await real_close(**kwargs)
        events.append("pg-ack")

    async def timed(owner, work, *, total_s):
        if total_s == 0.1:
            state["owners"].append(owner)
        return await real_call(owner, work, total_s=total_s)

    class Stream(AsyncMockStream):
        def __init__(self):
            super().__init__([])
            self.headers = False

        async def read(self, max_bytes, timeout=None):  # noqa: ASYNC109 — httpcore transport API
            if not self.headers:
                self.headers = True
                if state["mode"] == "response":
                    return b"HTTP/1.1 200 OK\r\nContent-Length: 100000000\r\n\r\n"
                status = b"503 Unavailable" if state["mode"] == "refusal" else b"200 OK"
                body = json.dumps(
                    {"error": "standby stalled", "code": "BACKEND_UNAVAILABLE"}
                    if state["mode"] == "refusal"
                    else {"org": "acme", "cell": "eu-1", "deployment_id": "dep-eu-1"}
                ).encode()
                # A timely reply may exceed the separate local phase allowance.
                await asyncio.sleep(0.04)
                return (
                    b"HTTP/1.1 "
                    + status
                    + b"\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
            entered.set()
            state["reads"].append(timeout)
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.sleep(0.002)
                return b" "
            except asyncio.CancelledError:
                events.append("http-cancel")
                raise

    class Backend(AsyncMockBackend):
        async def connect_tcp(self, *args, **kwargs):
            return Stream()

    class Client(actual_client):
        async def __aexit__(self, *args):
            if state["mode"] == "client-exit":
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    events.append("http-cancel")
                    raise
            return await super().__aexit__(*args)

    # TLS/pool setup is not part of the inert exchange being timed.
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(network_backend=Backend([]))

    def client(**kwargs):
        return Client(transport=transport, **kwargs)

    conn.close = close
    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(cell_place, "_directory", _DIRECTORY)
    monkeypatch.setattr(cell_place, "REQUEST_S", 0.1, raising=False)
    monkeypatch.setattr(cell_control.Owner, "call", timed)
    monkeypatch.setattr(settings, "router_operator_token", "inert-test-token")
    yield conn, events, entered, state
    leaked = asyncio.all_tasks() - before - {asyncio.current_task()}
    for task in leaked:
        task.cancel()
    await asyncio.gather(*leaked, return_exceptions=True)
    assert not leaked, "the exchange leaves no task on its private loop"


@pytest.mark.parametrize("blocking", ["response", "client-exit"])
@pytest.mark.parametrize("stop", ["deadline", "cancel"])
async def test_whole_exchange_closes_pg_before_http_cancellation(exchange, blocking, stop):
    conn, events, entered, state = exchange
    state["mode"] = blocking
    started = time.monotonic()
    task = asyncio.create_task(invoke_place())
    try:
        await asyncio.wait_for(entered.wait(), 0.2)
        if stop == "cancel":
            task.cancel("operator stop")
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert done, "per-read HTTP timeouts cannot bound a continuing response or client cleanup"
        with pytest.raises(
            TimeoutError if stop == "deadline" else asyncio.CancelledError
        ) as caught:
            await task
        if stop == "cancel":
            assert str(caught.value) == "operator stop"
        assert time.monotonic() - started < 0.3
        assert conn.closed and not conn.committed and not conn.terminated
        assert not any("INSERT" in call for call in conn.calls)
        assert events.index("pg-ack") < events.index("http-cancel")
        assert events.count("pg-close") == 1
        if stop == "deadline":
            (owner,) = state["owners"]
            assert owner.close_end == owner.phase_end
        if blocking == "response":
            assert state["reads"] and all(value == 0.1 for value in state["reads"])
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["success", "refusal"])
async def test_timely_actual_http_reply_keeps_local_phase_bounds(exchange, mode):
    conn, events, _, state = exchange
    state["mode"] = mode
    if mode == "refusal":
        with pytest.raises(cell_place.PlacementRefused, match="standby stalled"):
            await invoke_place()
        assert not conn.committed
    else:
        assert (await invoke_place())["deployment_id"] == "dep-eu-1"
        assert conn.committed
    assert conn.closed and not conn.terminated
    assert events.count("pg-close") == 1
