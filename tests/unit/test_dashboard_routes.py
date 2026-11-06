"""The dashboard is served at the server root without taking any API answer:
only its views' paths are pages, and any other path keeps the router's 404."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.unit.security_headers import assert_security_headers

pytestmark = pytest.mark.skipif(
    os.environ.get("LOOM_SKIP_APP_IMPORT") == "1", reason="app import disabled"
)

_PAGE_MARK = "dashboard-test-page"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from starlette.testclient import TestClient

    from server.identity.app import dashboard_routes
    from server.main import app
    from server.settings import settings

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(f"<html><head><title>{_PAGE_MARK}</title></head></html>")
    (tmp_path / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "assets" / "index-deadbeef.js").write_text("// js")
    monkeypatch.setattr(dashboard_routes, "_FRONTEND_DIST", tmp_path)
    monkeypatch.setattr(_assets_mount(app).app, "all_directories", [tmp_path / "assets"])
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "dashboard_redirect_url", "")
    return TestClient(app, base_url="http://localhost:7777")


def _assets_mount(app):
    return next(r for r in app.router.routes if getattr(r, "path", "") == "/assets")


def _state_ts_page_segments() -> set[str]:
    """`viewToPath(v)`'s first segment for every view in frontend/src/lib/state.ts."""
    src = (Path(__file__).resolve().parents[2] / "frontend/src/lib/state.ts").read_text()
    views = re.findall(
        r'"([a-z_:]+)"', re.search(r"ALL_VIEWS: View\[\] = \[(.*?)\];", src, re.S)[1]
    )
    canonical = dict(
        re.findall(
            r"(\w+): \"([a-z/_-]+)\"", re.search(r"CANONICAL_PATH[^{]*\{(.*?)\};", src, re.S)[1]
        )
    )
    return {canonical.get(v, v.replace(":", "/")).split("/", 1)[0] for v in views}


def test_page_segments_are_the_frontend_views() -> None:
    from server.identity.app.dashboard_routes import PAGE_SEGMENTS

    assert _state_ts_page_segments() == set(PAGE_SEGMENTS)


def test_no_api_route_uses_a_page_segment() -> None:
    from server.identity.app.dashboard_routes import PAGE_SEGMENTS
    from server.main import app

    for route in app.router.routes:
        if (
            getattr(route, "endpoint", None) is not None
            and route.endpoint.__name__ == "dashboard_page"
        ):
            continue
        segment = str(getattr(route, "path", "")).lstrip("/").split("/", 1)[0]
        assert segment not in PAGE_SEGMENTS, route.path


@pytest.mark.parametrize(
    "path,status,body",
    [
        ("/mcp", 405, {"detail": "Method Not Allowed"}),
        ("/v1/memory.search", 405, {"detail": "Method Not Allowed"}),
        ("/v1/x", 404, {"detail": "Not Found"}),
        ("/v1", 404, {"detail": "Not Found"}),
        # OAuth is off in unit tests: the discovery probes keep their JSON 404
        ("/.well-known/openid-configuration", 404, {"detail": "Not Found"}),
        ("/.well-known/oauth-protected-resource/mcp", 404, {"detail": "Not Found"}),
        ("/oauth/authorize", 404, {"detail": "Not Found"}),
        ("/assets/missing.js", 404, None),
        # not a view: the JSON 404, never the page (an LB probing /health)
        ("/health", 404, {"detail": "Not Found"}),
        ("/metrics", 404, {"detail": "Not Found"}),
        ("/robots.txt", 404, {"detail": "Not Found"}),
        ("/memoriesx", 404, {"detail": "Not Found"}),
        ("/me/", 404, {"detail": "Not Found"}),  # `/me` alone is the sign-in landing
        ("/me/tokens", 404, {"detail": "Not Found"}),
    ],
)
def test_api_answers_are_unchanged(client, path: str, status: int, body) -> None:
    r = client.get(path, follow_redirects=False)
    assert r.status_code == status, r.text
    assert _PAGE_MARK not in r.text
    if body is not None:
        assert r.json() == body


@pytest.mark.parametrize(
    "path,statuses",
    [
        ("/v1/health", (200, 503)),
        ("/v1/task/watch", (401, 422)),
        ("/v1/whoami", (200,)),
        ("/v1/.well-known/jwks.json", (200,)),
        ("/v1/knowledge.download", (401, 422)),
        ("/v1/operations", (200,)),
        ("/v1/api/openapi.json", (200,)),
        ("/openapi.json", (200,)),
    ],
)
def test_get_routes_still_answer_json(client, path: str, statuses: tuple[int, ...]) -> None:
    r = client.get(path, follow_redirects=False)
    assert r.status_code in statuses, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert _PAGE_MARK not in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/me",
        "/memories",
        "/memories/",
        "/memories/m-1",
        "/admin/review_queue",
        "/settings?tab=tokens",
    ],
)
def test_page_paths_serve_the_dashboard(client, path: str) -> None:
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200
    assert _PAGE_MARK in r.text and "window.__LOOM_AUTH_DISABLED" in r.text
    assert r.headers["cache-control"] == "no-store"
    [csp] = r.headers.get_list("content-security-policy")
    assert "nonce-" in csp
    # No window of another origin keeps a handle on the dashboard to navigate it.
    assert r.headers["cross-origin-opener-policy"] == "same-origin"


def test_the_sign_in_landing_never_follows_the_aggregator_redirect(client, monkeypatch) -> None:
    """A sign-in lands on `/me#token=…`; the browser would carry that fragment
    through a redirect, so `/me` serves the page whatever the root does."""
    from server.settings import settings

    monkeypatch.setattr(settings, "dashboard_redirect_url", "https://aggregator.example/")
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 302 and root.headers["location"] == "https://aggregator.example/"
    landing = client.get("/me", follow_redirects=False)
    assert landing.status_code == 200 and _PAGE_MARK in landing.text


def test_assets_are_served_immutable(client) -> None:
    r = client.get("/assets/index-deadbeef.js")
    assert r.status_code == 200 and r.text == "// js"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/v1/whoami").headers.get("cache-control") != (
        "public, max-age=31536000, immutable"
    )


def test_favicon_is_an_svg(client) -> None:
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")


def test_a_missing_build_answers_503_on_pages(client, tmp_path, monkeypatch) -> None:
    from server.identity.app import dashboard_routes
    from server.main import app

    monkeypatch.setattr(dashboard_routes, "_FRONTEND_DIST", tmp_path / "absent")
    monkeypatch.setattr(_assets_mount(app).app, "all_directories", [tmp_path / "absent/assets"])
    assert client.get("/memories").status_code == 503
    assert client.get("/assets/index-deadbeef.js").status_code == 404


@pytest.mark.parametrize("path", ["/v1/whoami", "/", "/assets/index-deadbeef.js"])
def test_api_page_and_asset_answers_carry_the_browser_security_headers(client, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert_security_headers(r)


def test_hsts_max_age_zero_sends_no_hsts(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "hsts_max_age_seconds", 0)
    r = client.get("/v1/whoami")
    assert "strict-transport-security" not in r.headers
    assert r.headers["x-frame-options"] == "DENY"
