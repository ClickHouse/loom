"""The relay inside a repository-job sandbox bridges at most a fixed number of
connections at a time, and a thread it cannot start or a failed accept costs at
most one connection, never the relay: a client opening hundreds at once (pnpm's
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


@pytest.mark.parametrize("failing_start", [1, 2], ids=["the bridge's", "its second pipe's"])
def test_a_thread_that_cannot_start_costs_one_connection(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, failing_start: int
) -> None:
    starts = 0

    class Thread(threading.Thread):
        def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts == failing_start:
                raise RuntimeError("can't start new thread")
            super().start()

    stand_in = types.SimpleNamespace(Thread=Thread)
    monkeypatch.setattr(egress_relay, "threading", stand_in)
    with _relay(upstream.server.getsockname(), 1) as port:
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert first.recv(1) == b""  # closed: one of its threads could not start
        first.close()
        if failing_start == 2:  # its bridge had reached the proxy
            assert upstream.arrived.acquire(timeout=5)
        second = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)  # its slot came back
        second.close()


def test_an_accept_that_fails_is_retried(upstream: _Upstream) -> None:
    with _relay(upstream.server.getsockname(), 1, _FlakyListener) as port:
        client = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)
        client.close()
