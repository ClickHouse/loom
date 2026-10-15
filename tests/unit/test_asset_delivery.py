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


def test_hashed_assets_are_immutable_for_a_year(tmp_path, monkeypatch) -> None:
    """The header rides any 200 under /assets/ — exercised through the
    middleware with a route mounted for the test, since CI has no built
    frontend/dist."""
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from server.main import app

    marker = "/assets/test-only-deadbeef.js"
    if not any(getattr(r, "path", "") == marker for r in app.routes):
        # ahead of the page catch-all and the real assets mount
        app.router.routes.insert(
            0,
            Route(marker, lambda request: PlainTextResponse("// js", media_type="text/javascript")),
        )

    r = TestClient(app).get(marker)
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "public, max-age=31536000, immutable"
    # and a non-asset route keeps whatever it had (no header, or its own)
    r2 = TestClient(app).get("/v1/whoami")
    assert r2.headers.get("cache-control") != "public, max-age=31536000, immutable"
