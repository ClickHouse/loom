"""The dashboard's bytes arrive compressed and its
hashed assets are cached for a year."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LOOM_SKIP_APP_IMPORT") == "1", reason="app import disabled"
)


def _client():
    from starlette.testclient import TestClient

    from server.main import app

    return TestClient(app)


def test_large_json_answers_are_gzipped_when_the_browser_accepts_it() -> None:
    c = _client()
    r = c.get("/v1/operations", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert len(r.json()) > 100  # the registry: well above the 1 KiB floor


def test_small_answers_are_left_alone() -> None:
    c = _client()
    r = c.get("/v1/whoami", headers={"Accept-Encoding": "gzip"})
    assert r.status_code in (200, 401)
    assert r.headers.get("content-encoding") is None


def test_server_origin_is_the_dashboard() -> None:
    c = _client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (200, 503)  # 503: frontend/dist not built
    assert "location" not in r.headers
