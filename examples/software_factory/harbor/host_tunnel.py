"""Forward authenticated TLS tunnels through the host without reading TLS content."""

from __future__ import annotations

import base64
import contextlib
import http.server
import secrets
import select
import socket
import string
import threading

HOST_CHARACTERS = frozenset(string.ascii_letters + string.digits + ".-")


class HostTunnel:
    def __init__(self, *, connect=socket.create_connection):
        self.connect = connect
        self.token = secrets.token_urlsafe(32)
        self.authorization = (
            "Basic " + base64.b64encode(("factory:" + self.token).encode()).decode()
        )
        self.sockets, self.lock = set(), threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_CONNECT(self):
                if not secrets.compare_digest(
                    self.headers.get("Proxy-Authorization", ""), owner.authorization
                ):
                    self.send_error(407)
                    return
                host = self.path[:-4] if self.path.endswith(":443") else ""
                if not host or len(host) > 253 or any(c not in HOST_CHARACTERS for c in host):
                    self.send_error(403)
                    return
                try:
                    upstream = owner.connect((host, 443), timeout=20)
                except OSError:
                    self.send_error(502)
                    return
                with owner.lock:
                    owner.sockets.update((upstream, self.connection))
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.flush()
                    for stream in (upstream, self.connection):
                        stream.settimeout(30)
                    while True:
                        readable, _, _ = select.select((upstream, self.connection), (), (), 30)
                        if not readable:
                            return
                        for stream in readable:
                            data = stream.recv(65536)
                            if not data:
                                return
                            (upstream if stream is self.connection else self.connection).sendall(
                                data
                            )
                except OSError:
                    return
                finally:
                    upstream.close()
                    with owner.lock:
                        owner.sockets.difference_update((upstream, self.connection))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://factory:{self.token}@host.docker.internal:{self.server.server_port}"

    @property
    def host_url(self):
        return f"http://factory:{self.token}@127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            sockets = tuple(self.sockets)
        for stream in sockets:
            with contextlib.suppress(OSError):
                stream.shutdown(socket.SHUT_RDWR)
            stream.close()
        self.thread.join()
