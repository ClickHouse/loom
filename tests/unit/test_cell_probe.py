"""The probe route (server/core/cell_probe.py): the main port answers the in-memory
deployment id alone; the probe listener adds the nonces and is rate-limited."""

from __future__ import annotations

import asyncio
import errno
import logging
import signal
import socket
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import health
from server.core import cell, cell_probe
from server.settings import settings
from tests.unit.clock_fake import ManualClock


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(cell, "deployment_id", lambda: "dep-1")
    return "dep-1"


def _main() -> TestClient:
    app = FastAPI()
    health.attach(app)
    return TestClient(app)


@pytest.mark.parametrize("host", ["loom.example", "anything.test", "127.0.0.1"])
def test_the_main_port_answers_the_id_in_memory_whatever_the_host(
    deployment: str, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    async def _never() -> Any:
        pytest.fail("the main port read a store")

    monkeypatch.setattr(cell_probe, "nonces", _never)
    r = _main().get(cell_probe.PATH, headers={"Host": host})
    assert r.status_code == 200 and r.json() == {"deployment_id": deployment}
    assert r.headers["cache-control"] == "no-store"


def test_before_boot_verifies_it_the_main_port_answers_no_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cell, "deployment_id", lambda: "")
    assert _main().get(cell_probe.PATH).json() == {"deployment_id": ""}


def test_the_listener_answers_the_id_and_each_stores_nonce(
    deployment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _postgres() -> str:
        return "n-pg"

    async def _clickhouse() -> str:
        return "n-ch"

    async def _bucket() -> None:
        return None

    monkeypatch.setattr(cell_probe, "_postgres", _postgres)
    monkeypatch.setattr(cell_probe, "_clickhouse", _clickhouse)
    monkeypatch.setattr(cell_probe, "_bucket", _bucket)
    r = TestClient(cell_probe.listener_app()).get(cell_probe.PATH)
    assert r.status_code == 200
    assert r.json() == {
        "deployment_id": deployment,
        "nonces": {"postgres": "n-pg", "clickhouse": "n-ch", "bucket": None},
    }


def test_a_store_the_listener_cannot_read_answers_503(
    deployment: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _down() -> Any:
        raise OSError("refused")

    monkeypatch.setattr(cell_probe, "_clickhouse", _down)

    async def _ok() -> str:
        return "n"

    monkeypatch.setattr(cell_probe, "_postgres", _ok)
    with caplog.at_level(logging.WARNING, logger="loom.cell_probe"):
        r = TestClient(cell_probe.listener_app()).get(cell_probe.PATH)
    assert r.status_code == 503 and r.json()["store"] == "clickhouse"
    assert "clickhouse nonce could not be read: OSError: refused" in caplog.text
    assert r.json()["error"] == "clickhouse could not be read (OSError)"


def test_the_listener_answers_429_past_its_rate_and_again_a_minute_on(
    deployment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _none() -> dict[str, None]:
        return {"postgres": None, "clickhouse": None, "bucket": None}

    monkeypatch.setattr(cell_probe, "nonces", _none)
    clock = ManualClock()
    client = TestClient(cell_probe.listener_app(cell_probe._Rate(3, clock=clock)))
    assert [client.get(cell_probe.PATH).status_code for _ in range(4)] == [200, 200, 200, 429]
    refused = client.get(cell_probe.PATH)
    assert refused.status_code == 429 and 1 <= int(refused.headers["retry-after"]) <= 61
    clock.advance(60)
    assert client.get(cell_probe.PATH).status_code == 200


def test_every_web_process_of_a_host_can_listen_on_the_port() -> None:
    first = cell_probe.bind(0)  # port 0: the system picks one no other test holds
    first.listen()
    second = cell_probe.bind(first.getsockname()[1])  # a second process, while it listens
    second.listen()
    first.close()
    second.close()


def test_a_host_without_ipv6_binds_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    """IPv6 off by sysctl: the socket opens but `::` will not bind, so it takes 0.0.0.0."""
    real = socket.socket

    class _NoIPv6(real):  # type: ignore[misc,valid-type]
        def bind(self, address: Any) -> None:
            if self.family == socket.AF_INET6:
                raise OSError(errno.EADDRNOTAVAIL, "no IPv6")
            super().bind(address)

    monkeypatch.setattr(cell_probe.socket, "socket", _NoIPv6)
    sock = cell_probe.bind(0)
    try:
        assert sock.family == socket.AF_INET and sock.getsockname()[0] == "0.0.0.0"
    finally:
        sock.close()


def test_a_port_it_cannot_bind_is_logged_and_the_process_serves_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with socket.socket() as taken:  # held without SO_REUSEPORT: no one shares it
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        monkeypatch.setattr(settings, "cell_probe_port", taken.getsockname()[1])
        assert cell_probe.start_listener() is None
    assert "could not bind" in caplog.text


async def _get_when_up(port: int) -> httpx.Response:
    """The probe route's answer on `port`, once the listener there accepts."""
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                return await client.get(f"http://127.0.0.1:{port}{cell_probe.PATH}")
            except httpx.ConnectError:
                await asyncio.sleep(0.02)
    pytest.fail("the listener never accepted")


def test_start_listener_binds_the_configured_port_and_serves(
    deployment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start_listener` asks for LOOM_CELL_PROBE_PORT and serves on what it bound (port 0
    stands in for it, so no other test can hold it)."""

    async def _none() -> dict[str, None]:
        return {"postgres": None, "clickhouse": None, "bucket": None}

    monkeypatch.setattr(cell_probe, "nonces", _none)
    monkeypatch.setattr(settings, "cell_probe_port", 7790)
    asked: list[int] = []
    bound: list[socket.socket] = []
    real = cell_probe.bind

    def _bind(port: int) -> socket.socket:
        asked.append(port)
        bound.append(real(0))
        return bound[-1]

    monkeypatch.setattr(cell_probe, "bind", _bind)

    async def _run() -> int:
        listener = cell_probe.start_listener()
        assert listener is not None
        port = bound[0].getsockname()[1]
        try:
            return (await _get_when_up(port)).status_code
        finally:
            await listener.stop()

    assert asyncio.run(_run()) == 200
    assert asked == [7790]


def test_the_listener_serves_on_its_port_and_leaves_the_signal_handlers(
    deployment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _none() -> dict[str, None]:
        return {"postgres": None, "clickhouse": None, "bucket": None}

    monkeypatch.setattr(cell_probe, "nonces", _none)

    async def _run() -> dict[str, Any]:
        # Read inside the loop: asyncio.run installs its own SIGINT handler.
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        uvicorn_level = logging.getLogger("uvicorn.error").level
        sock = cell_probe.bind(0)  # port 0: the system picks one no other test holds
        port = sock.getsockname()[1]
        listener = cell_probe.Listener(sock)
        assert logging.getLogger("uvicorn.error").level == uvicorn_level
        try:
            r = await _get_when_up(port)
            assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
            return r.json()
        finally:
            await listener.stop()
            assert listener.task.done() and not listener.task.cancelled()

    assert asyncio.run(_run())["deployment_id"] == deployment


def test_the_bucket_nonce_is_the_object_under_the_prefix_and_none_without_it(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    """Prepare writes `<prefix>/_cell_probe`; the listener reads it back, and reads None
    once it is deleted or with no bucket at all."""
    from server.core import object_store
    from tests.unit.test_document_object_store import FakeS3

    fake = FakeS3()
    monkeypatch.setattr(object_store, "_client", lambda: fake)
    settings_override(document_s3_bucket="probe-bucket", document_s3_prefix="loom/originals")
    assert asyncio.run(cell_probe._bucket()) is None
    fake.objects[("probe-bucket", "loom/originals/_cell_probe")] = (b"n-bucket", None)
    assert asyncio.run(cell_probe._bucket()) == "n-bucket"
    settings_override(document_s3_bucket="")
    assert asyncio.run(cell_probe._bucket()) is None
