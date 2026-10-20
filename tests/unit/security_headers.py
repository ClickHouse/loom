"""The browser security headers server.main adds to every answer."""

from __future__ import annotations

import httpx

from server.settings import settings


def assert_security_headers(r: httpx.Response) -> None:
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["strict-transport-security"] == f"max-age={settings.hsts_max_age_seconds}"
