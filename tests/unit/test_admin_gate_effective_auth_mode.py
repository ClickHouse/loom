"""`/v1/admin/*` gates on the EFFECTIVE auth mode.

The transport reads `settings.auth_actually_required()`, which a
`single-user` identity provider forces to False, and `require_admin` must
read the same: an `.env` with `LOOM_AUTH_REQUIRED=true` plus
`LOOM_IDENTITY_PROVIDER=single-user` serves every route open, so the admin
surface must not answer 401.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.settings import settings


def _client() -> TestClient:
    from server.insights.app.admin_routes import admin_router

    app = FastAPI()
    app.include_router(admin_router)
    return TestClient(app)


def _probe(client: TestClient) -> int:
    # Any admin route will do: the gate runs before dispatch; 422 means the
    # gate let the (empty) body through to validation.
    return client.post("/v1/admin/budget.set", json={}).status_code


def test_single_user_mode_opens_admin_even_when_auth_required_flag_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    assert settings.auth_actually_required() is False
    assert _probe(_client()) != 401


def test_gateway_mode_keeps_admin_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "self_signup", "off")
    assert settings.auth_actually_required() is True
    r = _client().post("/v1/admin/budget.set", json={})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "AUTH_INVALID"


def test_gate_reads_the_effective_mode_not_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the contract: whatever `auth_actually_required()` says wins."""
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    assert _probe(_client()) == 401
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    assert _probe(_client()) != 401
