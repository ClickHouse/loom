"""Cell administration rejects ambiguous JSON without changing the byte-cap contract."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.insights.app import cell_admin_routes as routes
from server.insights.app.admin_routes import require_admin
from server.settings import settings
from tests.unit.principals import principal


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["", ',"context":null', ',"unused":NaN', ',"unused":1e999'])
async def test_authenticated_raw_json_is_strict_before_security_reads(monkeypatch, extra):
    auth = principal("platform_admin", subject="sa:automation", namespaces=(), token_id="token")
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    verified = AsyncMock()
    monkeypatch.setattr(routes, "verify_automation", verified)
    context = {
        "version": 1,
        "expected_cell": "alpha",
        "expected_deployment_id": "deployment",
        "expected_bot": "automation",
    }
    app = FastAPI()
    app.include_router(routes.cell_admin_router)

    async def authenticated():
        return auth

    app.dependency_overrides[require_admin] = authenticated
    raw = '{"context":' + json.dumps(context) + extra + "}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://cell.example"
    ) as client:
        response = await client.post(
            "/v1/admin/cell.capabilities",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == (400 if extra else 200)
    assert verified.await_count == int(not extra)
