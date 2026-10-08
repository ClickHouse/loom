"""A bare /me serves the dashboard so the client's landing rule chooses the
first view; a redirect to /me/setup would turn every visit into an explicit
Setup deep link."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.requests import Request

from server.identity.app import me_routes as me_mod
from server.settings import settings

pytestmark = pytest.mark.asyncio


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


async def test_bare_me_serves_the_spa_not_a_redirect() -> None:
    if not (me_mod._FRONTEND_DIST / "index.html").is_file():
        pytest.skip("frontend not built (npm --prefix frontend run build)")
    with (
        patch.object(settings, "auth_required", True),
        patch.object(settings, "identity_provider", "gateway"),
        patch.object(settings, "dashboard_redirect_url", ""),
    ):
        for path in ("/me", "/me/"):
            response = await me_mod.memory_dashboard(_request(path), spa_path="")
            assert response.status_code == 200, path
            assert "location" not in {k.lower() for k in response.headers}
            assert "window.__LOOM_AUTH_DISABLED" in response.body.decode()


async def test_federated_redirect_still_wins_on_bare_me() -> None:
    """The aggregator escape hatch (dashboard_redirect_url) is unchanged."""
    with (
        patch.object(settings, "auth_required", True),
        patch.object(settings, "dashboard_redirect_url", "https://agg.example/me"),
    ):
        response = await me_mod.memory_dashboard(_request("/me"), spa_path="")
    assert response.status_code == 302
    assert response.headers["location"] == "https://agg.example/me"
