"""The root serves the dashboard so the client's landing rule chooses the
first view; a redirect to /settings would turn every visit into an explicit
Settings deep link."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.requests import Request

from server.identity.app import dashboard_routes
from server.settings import settings

pytestmark = pytest.mark.asyncio


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


async def test_the_root_serves_the_spa_not_a_redirect() -> None:
    if not (dashboard_routes._FRONTEND_DIST / "index.html").is_file():
        pytest.skip("frontend not built (npm --prefix frontend run build)")
    with (
        patch.object(settings, "auth_required", True),
        patch.object(settings, "identity_provider", "gateway"),
        patch.object(settings, "dashboard_redirect_url", ""),
    ):
        response = await dashboard_routes.dashboard_page(_request("/"))
        assert response.status_code == 200
        assert "location" not in {k.lower() for k in response.headers}
        assert "window.__LOOM_AUTH_DISABLED" in response.body.decode()


async def test_federated_redirect_still_wins_on_the_root() -> None:
    """The aggregator escape hatch (dashboard_redirect_url) is unchanged."""
    with (
        patch.object(settings, "auth_required", True),
        patch.object(settings, "dashboard_redirect_url", "https://agg.example/loom"),
    ):
        response = await dashboard_routes.dashboard_page(_request("/"))
    assert response.status_code == 302
    assert response.headers["location"] == "https://agg.example/loom"
