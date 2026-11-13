"""The relay inside a repository-job sandbox: a bounded number of bridges, and a
failure that costs at most one connection, never the relay."""

from __future__ import annotations

import contextlib
import errno
import socket
import struct
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

from server.code.app.index import egress, egress_relay


@pytest.fixture(autouse=True)
def _short_linger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress_relay, "_LINGER_S", 0.2)


class _Upstream:
    """A unix server standing in for the egress proxy: it holds every connection
    open and counts them."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(128)
        self.held: list[socket.socket] = []
        self.arrived = threading.Semaphore(0)
        self.stop = threading.Event()
        self.accepting = threading.Thread(target=self._accept, daemon=True)
        self.accepting.start()

    def _accept(self) -> None:
        self.server.settimeout(0.05)
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            conn.settimeout(None)
            self.held.append(conn)
            self.arrived.release()

    def close(self) -> None:
        self.stop.set()
        self.accepting.join(timeout=5)
        self.server.close()
        for conn in self.held:
            conn.close()


class _Stopped(Exception):
    pass


class _Listener:
    """The relay's listener as the tests drive it: `stop` ends the accept loop, and
    with `fail_first` the first accept fails as it does when the host is out of
    descriptors."""

    def __init__(self, fail_first: bool = False) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.fail_first = fail_first

    def accept(self) -> tuple[socket.socket, Any]:
        if self.fail_first:
            self.fail_first = False
            raise OSError(errno.ENFILE, "Too many open files in system")
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                conn, address = self.sock.accept()
                conn.settimeout(None)
                return conn, address
        raise _Stopped


@contextlib.contextmanager
def _relay(
    sock_dir: Path, slots: int, listener: _Listener | None = None, idle_s: float = 30.0
) -> Iterator[tuple[int, _Upstream]]:
    """Run the relay's accept loop to a stand-in proxy; yields the relay's port and
    the proxy. On the way out the proxy closes, which ends every bridge, and the loop
    is joined."""
    upstream = _Upstream(str(sock_dir / "p.sock"))
    listener = listener or _Listener()
    slots_left = threading.BoundedSemaphore(slots)

    def run() -> None:
        try:
            egress_relay.accept_loop(listener, upstream.path, slots_left, idle_s)
        except _Stopped:
            slots_left.release()  # the one it took for that accept

    loop = threading.Thread(target=run, daemon=True)
    loop.start()
    try:
        yield listener.port, upstream
    finally:
        listener.stop.set()
        upstream.close()
        loop.join(timeout=5)
        listener.sock.close()
        # every bridge has ended once each slot is back
        bridges_done = all(slots_left.acquire(timeout=5) for _ in range(slots))
    assert not loop.is_alive()
    assert not upstream.accepting.is_alive()
    assert bridges_done


def _thread_failing_on(nth: int) -> types.SimpleNamespace:
    """A stand-in for the relay's `threading` whose `nth` thread start fails.
    The relay must not count on restarting a Thread whose start failed, so this one
    refuses too."""
    starts = 0

    class Thread(threading.Thread):
        failed = False

        def start(self) -> None:
            nonlocal starts
            if self.failed:
                raise RuntimeError("threads can only be started once")
            starts += 1
            if starts == nth:
                self.failed = True
                raise RuntimeError("can't start new thread")
            super().start()

    return types.SimpleNamespace(Thread=Thread, Event=threading.Event)


def _socket_failing_once() -> types.SimpleNamespace:
    """A stand-in for the relay's `socket` whose first socket() fails."""
    calls = 0

    def make(*args: Any) -> socket.socket:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ENFILE, "Too many open files in system")
        return socket.socket(*args)

    names = ("AF_UNIX", "SOCK_STREAM", "SHUT_WR")
    return types.SimpleNamespace(socket=make, **{n: getattr(socket, n) for n in names})


def test_connections_past_the_cap_wait_until_a_bridge_ends(sock_dir: Path) -> None:
    with _relay(sock_dir, 2) as (port, upstream):
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


@pytest.mark.parametrize("failing_start", [1, 2], ids=["the bridge", "its second pipe"])
def test_a_thread_that_cannot_start_is_started_again(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch, failing_start: int
) -> None:
    monkeypatch.setattr(egress_relay, "threading", _thread_failing_on(failing_start))
    with _relay(sock_dir, 1) as (port, upstream):
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert upstream.arrived.acquire(timeout=5)
        upstream.held[0].sendall(b"hi")
        assert client.recv(2) == b"hi"
        client.sendall(b"yo")
        assert upstream.held[0].recv(2) == b"yo"
        client.close()


def test_a_bridge_whose_client_reset_holds_its_slot_while_the_proxy_is_silent(
    sock_dir: Path,
) -> None:
    """The proxy holds its slot until it closes; a relay slot freed sooner would send
    the next connection to a full proxy, to be refused."""
    with _relay(sock_dir, 1) as (port, upstream):
        first = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)
        first.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        first.close()  # a reset
        second = socket.create_connection(("127.0.0.1", port))
        assert not upstream.arrived.acquire(timeout=0.5)
        upstream.held[0].close()  # the proxy frees its slot
        assert upstream.arrived.acquire(timeout=5)
        second.close()


def test_a_bridge_whose_client_reset_ends_while_the_proxy_still_sends(sock_dir: Path) -> None:
    """The rest of a download nobody will read is not fetched: the proxy's next write
    fails, and it frees its slot as the relay does."""
    with _relay(sock_dir, 1) as (port, upstream):
        first = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)
        first.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        first.close()  # a reset
        upstream.held[0].settimeout(5)
        with pytest.raises(OSError):  # the relay has gone: the proxy's write fails
            for _ in range(10_000):
                upstream.held[0].sendall(b"x" * 65536)
        second = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)
        second.close()


def test_a_client_left_open_after_the_proxy_closed_frees_its_slot(sock_dir: Path) -> None:
    """A client may keep a finished connection in its pool and never close it."""
    with _relay(sock_dir, 1) as (port, upstream):
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert upstream.arrived.acquire(timeout=5)
        upstream.held[0].close()  # the proxy ends the tunnel
        assert first.recv(1) == b""
        second = socket.create_connection(("127.0.0.1", port))  # first is still open
        assert upstream.arrived.acquire(timeout=5)
        first.close()
        second.close()


def test_a_client_still_sending_after_the_proxy_closed_frees_its_slot(sock_dir: Path) -> None:
    with _relay(sock_dir, 1) as (port, upstream):
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert upstream.arrived.acquire(timeout=5)
        upstream.held[0].close()  # the proxy ends the tunnel
        assert first.recv(1) == b""
        stop = threading.Event()

        def keep_sending() -> None:
            with contextlib.suppress(OSError):  # the bridge may reset it once it is gone
                while not stop.is_set():
                    first.sendall(b"x")
                    time.sleep(0.05)

        sender = threading.Thread(target=keep_sending)
        sender.start()
        try:
            second = socket.create_connection(("127.0.0.1", port))
            assert upstream.arrived.acquire(timeout=5)
            second.close()
        finally:
            stop.set()
            sender.join()
            first.close()


def test_a_client_still_uploading_to_a_half_closed_proxy_keeps_its_bridge(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server has sent EOF and still reads: each byte restarts the linger."""
    monkeypatch.setattr(egress_relay, "_LINGER_S", 1.0)  # 0.5 s either side of a gap
    with _relay(sock_dir, 1) as (port, upstream):
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert upstream.arrived.acquire(timeout=5)
        upstream.held[0].shutdown(socket.SHUT_WR)  # the proxy's side has ended
        assert client.recv(1) == b""
        chunks = 3
        for _ in range(chunks):  # each gap longer than one linger, shorter than two
            client.sendall(b"x")
            time.sleep(1.5 * egress_relay._LINGER_S)
        received = b""
        upstream.held[0].settimeout(5)
        while len(received) < chunks and (piece := upstream.held[0].recv(chunks)):
            received += piece
        assert received == b"x" * chunks
        client.close()


def test_a_client_that_never_reads_gives_its_slot_back_and_is_reset(sock_dir: Path) -> None:
    with _relay(sock_dir, 1, idle_s=0.3) as (port, upstream):
        first = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)

        def send_more_than_any_buffer_holds() -> None:
            with contextlib.suppress(OSError):
                upstream.held[0].sendall(b"x" * (64 * 1024 * 1024))
                upstream.held[0].shutdown(socket.SHUT_WR)

        sender = threading.Thread(target=send_more_than_any_buffer_holds)
        sender.start()
        try:
            second = socket.create_connection(("127.0.0.1", port))
            assert upstream.arrived.acquire(timeout=5)  # the stuck write gave up
            second.close()
            first.settimeout(5)
            with pytest.raises(ConnectionResetError):  # not a clean end of a cut stream
                while first.recv(1 << 20):
                    pass
        finally:
            first.close()
            sender.join(timeout=10)


def test_an_upstream_socket_that_cannot_open_costs_its_connection_only(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(egress_relay, "socket", _socket_failing_once())
    with _relay(sock_dir, 1) as (port, upstream):
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert first.recv(1) == b""  # closed
        first.close()
        second = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)  # its slot came back
        second.close()


def test_an_accept_that_fails_is_retried(sock_dir: Path) -> None:
    with _relay(sock_dir, 1, _Listener(fail_first=True)) as (port, upstream):
        client = socket.create_connection(("127.0.0.1", port))
        assert upstream.arrived.acquire(timeout=5)
        client.close()


def test_the_relay_bridges_as_many_connections_as_the_proxy_holds_tunnels() -> None:
    assert egress_relay.MAX_BRIDGES == egress.MAX_TUNNELS


def test_serve_takes_the_proxys_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    served = []

    def serve(*args: object) -> NoReturn:
        served.append(args)
        raise SystemExit(0)  # the real one execs ARGV

    monkeypatch.setattr(egress_relay, "serve", serve)
    with pytest.raises(SystemExit):
        egress_relay.main(["serve", "3128", "/run/loom-egress/proxy.sock", "300.0", "--", "true"])
    assert served == [(3128, "/run/loom-egress/proxy.sock", 300.0, ["true"])]


def test_serve_without_the_idle_timeout_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert egress_relay.main(["serve", "3128", "/run/loom-egress/proxy.sock", "--", "true"]) == 2
    assert "serve PORT SOCKET IDLE -- ARGV" in capsys.readouterr().err


def test_an_idle_timeout_past_what_a_socket_takes_means_none(sock_dir: Path) -> None:
    with _relay(sock_dir, 1, idle_s=float("inf")) as (port, upstream):
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert upstream.arrived.acquire(timeout=5)
        upstream.held[0].sendall(b"hi")
        assert client.recv(2) == b"hi"
        client.close()
