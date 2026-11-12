"""The relay inside a repository-job sandbox (run as `python -I`, stdlib only).

The sandbox has no network. Its one way out is the egress proxy's unix socket,
mounted in; this script bridges to it so ordinary clients can use it:

  serve PORT SOCKET MAX -- ARGV...
                                listen on 127.0.0.1:PORT (git, go and npm use
                                it as their https proxy), then exec ARGV; at
                                most MAX connections are bridged at a time
  connect HOST PORT SOCKET      ssh's ProxyCommand: ask the proxy for HOST:PORT
                                and pipe stdin/stdout through the tunnel

It is not imported by the server; sandbox.py mounts this file into the sandbox.
"""

import os
import socket
import sys
import threading
import time


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _bridge(conn: socket.socket, path: str, slots: threading.Semaphore) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.connect(path)
            back = threading.Thread(target=_pipe, args=(upstream, conn), daemon=True)
            back.start()
            _pipe(conn, upstream)
            back.join()
    except (OSError, RuntimeError):  # RuntimeError: the job's task limit refused a thread
        pass
    finally:
        conn.close()
        slots.release()


def accept_loop(listener: socket.socket, path: str, slots: threading.Semaphore) -> None:
    """Bridge each connection in two threads, as many at a time as `slots` allows;
    the rest wait in the listen backlog."""
    while True:
        slots.acquire()
        try:
            conn, _ = listener.accept()
        except OSError:  # ENFILE, ECONNABORTED and the like: retried
            slots.release()
            if listener.fileno() == -1:  # the listener was closed: how the tests stop it
                return
            time.sleep(0.1)
            continue
        while True:
            # a new Thread each try: one whose start failed cannot be started again
            try:
                threading.Thread(target=_bridge, args=(conn, path, slots), daemon=True).start()
                break
            except RuntimeError:  # the job is at its task limit: wait, as the backlog does
                time.sleep(0.1)


def serve(port: int, path: str, max_bridges: int, argv: list[str]) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(4096)  # the kernel caps it at net.core.somaxconn
    child = os.fork()
    if child == 0:
        # The relay is a grandchild handed to the sandbox's init, so ARGV has no
        # stray child; it dies with the sandbox's pid namespace.
        if os.fork() == 0:
            accept_loop(listener, path, threading.BoundedSemaphore(max_bridges))
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
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def main(args: list[str]) -> int:
    if len(args) >= 6 and args[0] == "serve" and args[4] == "--":
        serve(int(args[1]), args[2], int(args[3]), args[5:])
    if len(args) == 4 and args[0] == "connect":
        return connect(args[1], args[2], args[3])
    sys.stderr.write(
        "usage: egress_relay.py serve PORT SOCKET MAX -- ARGV | connect HOST PORT SOCK\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
