"""The browser security headers server.main adds to every answer."""

from __future__ import annotations

from typing import Any


def assert_security_headers(r: Any) -> None:
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["strict-transport-security"] == "max-age=31536000"
