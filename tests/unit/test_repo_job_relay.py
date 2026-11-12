"""The relay inside a repository-job sandbox bridges at most a fixed number of
connections at a time, and a thread it cannot start, a socket it cannot open or a
failed accept costs at most one connection, never the relay: a client opening hundreds at once (pnpm's
metadata check) would otherwise reach the job's task limit and leave the job
with no route out."""

from __future__ import annotations

import contextlib
import errno
import socket
import threading
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from server.code.app.index import egress_relay


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
        with contextlib.suppress(OSError):
            self.server.shutdown(socket.SHUT_RDWR)
        self.server.close()
        for conn in self.held:
            conn.close()


@pytest.fixture
def upstream(sock_dir: Path) -> Iterator[_Upstream]:
    proxy = _Upstream(str(sock_dir / "p.sock"))
    yield proxy
    proxy.close()


class _FlakyListener:
    """A listener whose first accept fails as if the job were out of descriptors."""

    def __init__(self, listener: socket.socket) -> None:
        self.listener = listener
        self.calls = 0

    def accept(self) -> tuple[socket.socket, object]:
        self.calls += 1
        if self.calls == 1:
            raise OSError(errno.EMFILE, "Too many open files")
        return self.listener.accept()

    def fileno(self) -> int:
        return self.listener.fileno()


@contextlib.contextmanager
def _relay(
    path: str, slots: int, accept_on: Callable[[socket.socket], Any] = lambda listener: listener
) -> Iterator[int]:
    """Run the relay's accept loop on a free port; yields the port."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    loop = threading.Thread(
        target=egress_relay.accept_loop,
        args=(accept_on(listener), path, threading.BoundedSemaphore(slots)),
        daemon=True,
    )
    loop.start()
    try:
        yield listener.getsockname()[1]
    finally:
        with contextlib.suppress(OSError):
            listener.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept
        listener.close()


def test_connections_past_the_cap_wait_until_a_bridge_ends(upstream: _Upstream) -> None:
    with _relay(upstream.server.getsockname(), 2) as port:
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


def _thread_failing_on(start: int) -> types.SimpleNamespace:
    """A stand-in for the relay's `threading` whose `start`-th thread start fails."""
    starts = 0

    class Thread(threading.Thread):
        def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts == start:
                raise RuntimeError("can't start new thread")
            super().start()

    return types.SimpleNamespace(Thread=Thread)


def _socket_failing_once() -> types.SimpleNamespace:
    """A stand-in for the relay's `socket` whose first socket() fails."""
    calls = 0

    def make(*args: Any) -> socket.socket:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ENFILE, "Too many open files in system")
        return socket.socket(*args)

    names = ("AF_UNIX", "SOCK_STREAM", "SHUT_WR", "SHUT_RDWR")
    return types.SimpleNamespace(socket=make, **{n: getattr(socket, n) for n in names})


def test_a_bridge_thread_that_cannot_start_is_retried(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(egress_relay, "threading", _thread_failing_on(1))
    with _relay(upstream.server.getsockname(), 1) as port:
        client = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)
        client.close()


@pytest.mark.parametrize("failure", ["its second pipe's thread", "its upstream socket"])
def test_a_failure_inside_a_bridge_costs_its_connection_only(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "its second pipe's thread":
        monkeypatch.setattr(egress_relay, "threading", _thread_failing_on(2))
    else:
        monkeypatch.setattr(egress_relay, "socket", _socket_failing_once())
    with _relay(upstream.server.getsockname(), 1) as port:
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert first.recv(1) == b""  # closed
        first.close()
        if failure == "its second pipe's thread":  # its bridge had reached the proxy
            assert upstream.arrived.acquire(timeout=5)
        second = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)  # its slot came back
        second.close()


def test_a_client_left_open_after_the_proxy_closed_frees_its_slot(upstream: _Upstream) -> None:
    with _relay(upstream.server.getsockname(), 1) as port:
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert upstream.arrived.acquire(timeout=5)
        upstream.held[0].close()  # the proxy ends the tunnel
        assert first.recv(1) == b""
        second = socket.create_connection(("127.0.0.1", port))  # first is still open
        assert upstream.arrived.acquire(timeout=5)
        first.close()
        second.close()


def test_an_accept_that_fails_is_retried(upstream: _Upstream) -> None:
    with _relay(upstream.server.getsockname(), 1, _FlakyListener) as port:
        client = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)
        client.close()
