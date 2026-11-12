"""The relay inside a repository-job sandbox bridges at most a fixed number of
connections at a time, and a thread it cannot start costs one connection, never
the relay: a client opening hundreds at once (pnpm's metadata check) would
otherwise reach the job's task limit and leave the job with no route out."""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from server.code.app.index import egress_relay


@pytest.fixture
def sock_path() -> Iterator[str]:
    """A short socket path: AF_UNIX paths are capped near 104 bytes on macOS."""
    short = Path(tempfile.mkdtemp(prefix="relay-", dir="/tmp"))
    yield str(short / "p.sock")
    shutil.rmtree(short, ignore_errors=True)


class _Upstream:
    """A unix server standing in for the egress proxy: it holds every connection
    open and counts them."""

    def __init__(self, path: str) -> None:
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(128)
        self.held: list[socket.socket] = []
        self.arrived = threading.Semaphore(0)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.held.append(conn)
            self.arrived.release()

    def close(self) -> None:
        self.server.close()
        for conn in self.held:
            conn.close()


def _relay(path: str, slots: int) -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    semaphore = threading.Semaphore(slots)
    threading.Thread(
        target=egress_relay.accept_loop, args=(listener, path, semaphore), daemon=True
    ).start()
    return listener, listener.getsockname()[1]


def test_connections_past_the_cap_wait_until_a_bridge_ends(sock_path: str) -> None:
    upstream = _Upstream(sock_path)
    listener, port = _relay(sock_path, 2)
    clients = [socket.create_connection(("127.0.0.1", port)) for _ in range(5)]
    try:
        assert upstream.arrived.acquire(timeout=5) and upstream.arrived.acquire(timeout=5)
        assert not upstream.arrived.acquire(timeout=0.3)  # three wait in the backlog
        for conn in [*clients[:2], *upstream.held[:2]]:  # both bridged connections end
            conn.close()
        assert upstream.arrived.acquire(timeout=5) and upstream.arrived.acquire(timeout=5)
        assert not upstream.arrived.acquire(timeout=0.3)  # still two at a time
    finally:
        for client in clients:
            client.close()
        listener.close()
        upstream.close()


def test_a_thread_that_cannot_start_costs_one_connection(
    sock_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = _Upstream(sock_path)
    real_start = threading.Thread.start
    refusals = [1]

    def start(self: threading.Thread) -> None:
        if self._target is egress_relay._bridge and refusals[0]:  # type: ignore[attr-defined]
            refusals[0] -= 1
            raise RuntimeError("can't start new thread")
        real_start(self)

    listener, port = _relay(sock_path, 4)
    monkeypatch.setattr(threading.Thread, "start", start)
    first = socket.create_connection(("127.0.0.1", port))
    try:
        first.settimeout(5)
        assert first.recv(1) == b""  # closed: its thread could not start
        second = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)  # the relay still serves
        second.close()
    finally:
        first.close()
        listener.close()
        upstream.close()
