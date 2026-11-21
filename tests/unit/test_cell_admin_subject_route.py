"""The exact subject adapter is exposed through the ordinary positive bot guard."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.insights.app import cell_admin_routes as routes
from server.settings import settings


@pytest.mark.asyncio
async def test_subject_route_refuses_authoff_before_business(monkeypatch):
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    business = AsyncMock(side_effect=AssertionError("business reached without authority"))
    monkeypatch.setattr(routes.security_actions, "subject_revoke", business)
    app = FastAPI()
    app.include_router(routes.cell_admin_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://cell.example"
    ) as client:
        response = await client.post("/v1/admin/cell.subject_revoke", json={})
    assert response.status_code == 403
    business.assert_not_awaited()


def test_capabilities_advertise_only_the_implemented_subject_and_blacklist_actions():
    assert "subject_revoke" in routes.AVAILABLE_ACTIONS
    assert {action for action in routes.AVAILABLE_ACTIONS if action.startswith("blacklist_")} == {
        "blacklist_global_add",
        "blacklist_global_list",
    }
