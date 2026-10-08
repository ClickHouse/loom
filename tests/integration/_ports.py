"""Kernel-assigned ports for the servers integration tests spawn.

Fixed ports collide under random test order (pytest-randomly):
two fixtures sharing one port can run back to back, and the second binds
while the first server is still releasing. Ask the kernel for a free port
per instance instead; the listener binds it right after.
"""

from __future__ import annotations

import socket


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
