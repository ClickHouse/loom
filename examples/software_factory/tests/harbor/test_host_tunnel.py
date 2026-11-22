"""Host tunnels authenticate clients and preserve bytes without TLS termination."""

import socket
import threading
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit

from examples.software_factory.harbor.host_tunnel import HostTunnel


class HostTunnelContracts(unittest.TestCase):
    def test_unauthenticated_clients_cannot_open_an_upstream(self):
        connect = Mock()
        with HostTunnel(connect=connect) as tunnel:
            host, container = urlsplit(tunnel.host_url), urlsplit(tunnel.url)
            self.assertEqual(host.hostname, "127.0.0.1")
            self.assertEqual(container.hostname, "host.docker.internal")
            self.assertEqual(host.port, container.port)
            self.assertEqual(host.password, container.password)
            with socket.create_connection(("127.0.0.1", tunnel.server.server_port)) as client:
                client.sendall(b"CONNECT example.com:443 HTTP/1.0\r\n\r\n")
                self.assertIn(b"407", client.recv(4096).splitlines()[0])
        connect.assert_not_called()

    def test_authenticated_tunnel_preserves_opaque_bytes_and_closes(self):
        upstream, provider = socket.socketpair()
        connect = Mock(return_value=upstream)

        def echo():
            with provider:
                while data := provider.recv(65536):
                    provider.sendall(data)

        thread = threading.Thread(target=echo)
        thread.start()
        with HostTunnel(connect=connect) as tunnel:
            with socket.create_connection(("127.0.0.1", tunnel.server.server_port)) as client:
                client.settimeout(2)
                client.sendall(
                    (
                        "CONNECT example.com:443 HTTP/1.0\r\nProxy-Authorization: "
                        + tunnel.authorization
                        + "\r\n\r\n"
                    ).encode()
                )
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    header += client.recv(1)
                self.assertIn(b"200", header.splitlines()[0])
                client.sendall(b"opaque-encrypted-record")
                self.assertEqual(client.recv(4096), b"opaque-encrypted-record")
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        connect.assert_called_once_with(("example.com", 443), timeout=20)
