"""The relay inside a repository-job sandbox (run as `python -I`, stdlib only).

The sandbox has no network. Its one way out is the egress proxy's unix socket,
mounted in; this script bridges to it so ordinary clients can use it:

  serve PORT SOCKET IDLE -- ARGV...
                                listen on 127.0.0.1:PORT (git, go and npm use
                                it as their https proxy), then exec ARGV; a write
                                to a client waits at most IDLE seconds
  connect HOST PORT SOCKET      ssh's ProxyCommand: ask the proxy for HOST:PORT
                                and pipe stdin/stdout through the tunnel

It is not imported by the server; sandbox.py mounts this file into the sandbox.
"""

import contextlib
import os
import select
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable

# not a setting: the proxy's MAX_TUNNELS (a unit test pins them equal); no job holds more
MAX_BRIDGES = 32
# A client's silence, once the proxy's side has ended, that ends its bridge.
# not a setting: a pause between writes, far below any timeout to tune
_LINGER_S = 1.0
# not a setting: a pause before trying again at a kernel limit
_RETRY_PAUSE_S = 0.1


def _to_client(src: socket.socket, dst: socket.socket, to_client_done: threading.Event) -> None:
    """Proxy to client, to the proxy's EOF or a failed write to the client. A write
    that timed out resets the client, which a clean end would leave unaware of the cut;
    the proxy's own EOF, even after the server reset, reaches it as an end."""
    how = socket.SHUT_WR
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except TimeoutError:
        dst.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        how = socket.SHUT_RD  # wakes _to_proxy and sends no FIN; the close then resets
    except OSError:
        pass
    finally:
        to_client_done.set()
        with contextlib.suppress(OSError):
            dst.shutdown(how)


def _to_proxy(src: socket.socket, dst: socket.socket, to_client_done: threading.Event) -> None:
    """Client to proxy, to the client's EOF, or to a silence once the proxy-to-client
    direction has ended: a client may keep a finished connection open. The silence
    is one to two `_LINGER_S`, and two after a byte from the client."""
    lingering = False
    try:
        while True:
            if not select.select([src], [], [], _LINGER_S)[0]:
                if lingering:
                    return
                lingering = to_client_done.is_set()  # a full wait from here on
                continue
            data = src.recv(65536)
            if not data:
                return
            dst.sendall(data)  # fails once the proxy has closed
            lingering = False
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


def _start(target: Callable[..., None], *args: object) -> threading.Thread:
    """Start a daemon thread, waiting while the job is at its task limit."""
    while True:
        # a new Thread each try: one whose start failed may not start again
        thread = threading.Thread(target=target, args=args, daemon=True)
        try:
            thread.start()
            return thread
        except RuntimeError:
            time.sleep(_RETRY_PAUSE_S)


def _bridge(conn: socket.socket, path: str, slots: threading.Semaphore, idle_s: float) -> None:
    try:
        with contextlib.suppress(OverflowError):  # too large for a socket (inf): none
            conn.settimeout(idle_s)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.connect(path)
            to_client_done = threading.Event()
            back = _start(_to_client, upstream, conn, to_client_done)
            _to_proxy(conn, upstream, to_client_done)
            back.join()
            # A client that reset leaves the proxy's side open: wait for its EOF, so the
            # slot is not freed while the proxy holds its own (the rest is dropped).
            while upstream.recv(65536):
                pass
    except OSError:
        pass
    finally:
        conn.close()
        slots.release()


def accept_loop(
    listener: socket.socket, path: str, slots: threading.Semaphore, idle_s: float
) -> None:
    """Bridge each connection in two threads, as many at a time as `slots` allows;
    the rest wait in the listen backlog."""
    while True:
        slots.acquire()
        try:
            conn, _ = listener.accept()
        except OSError:  # ENFILE, ECONNABORTED and the like
            slots.release()
            time.sleep(_RETRY_PAUSE_S)
            continue
        _start(_bridge, conn, path, slots, idle_s)


def serve(port: int, path: str, idle_s: float, argv: list[str]) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(4096)  # the kernel caps it at net.core.somaxconn
    child = os.fork()
    if child == 0:
        # The relay is a grandchild handed to the sandbox's init, so ARGV has no
        # stray child; it dies with the sandbox's pid namespace.
        if os.fork() == 0:
            accept_loop(listener, path, threading.BoundedSemaphore(MAX_BRIDGES), idle_s)
        os._exit(0)
    os.waitpid(child, 0)
    listener.close()
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        sys.stderr.write(f"{argv[0]}: {e.strerror}\n")
        os._exit(127)


def connect(host: str, port: str, path: str) -> int:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.connect(path)
    upstream.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        piece = upstream.recv(1)
        if not piece:
            break
        head += piece
    if not head.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
        sys.stderr.write(f"egress proxy refused {host}:{port}: {head[:200]!r}\n")
        return 1
    threading.Thread(target=_copy_stdin, args=(upstream,), daemon=True).start()
    while data := upstream.recv(65536):
        os.write(1, data)
    return 0


def _copy_stdin(dst: socket.socket) -> None:
    try:
        while data := os.read(0, 65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


def main(args: list[str]) -> int:
    if len(args) >= 6 and args[0] == "serve" and args[4] == "--":
        serve(int(args[1]), args[2], float(args[3]), args[5:])
    if len(args) == 4 and args[0] == "connect":
        return connect(args[1], args[2], args[3])
    sys.stderr.write(
        "usage: egress_relay.py serve PORT SOCKET IDLE -- ARGV | connect HOST PORT SOCK\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
