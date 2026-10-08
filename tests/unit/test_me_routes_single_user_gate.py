"""The /v1/me token routes follow the transport's auth decision:
identity_provider=single-user means no auth even when auth_required is left
true (a prod-derived dev .env), and a real auth-on deployment still demands
a bearer."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.identity.app.me_service_accounts as me
from server.identity.app import me_callers


class _CH:
    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return []

    async def query_one(self, sql: str, params: Any = None) -> Any:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())

    async def aliases(org: str, subject: str) -> list[str]:
        return [subject]

    monkeypatch.setattr(me_callers, "subject_aliases_of", aliases)
    app = FastAPI()
    app.include_router(me.router)
    return TestClient(app)


def test_single_user_mode_serves_without_a_bearer(client, monkeypatch) -> None:
    monkeypatch.setattr(me.settings, "auth_required", True)
    monkeypatch.setattr(me.settings, "identity_provider", "single-user")
    r = client.get("/v1/me/service_accounts")
    assert r.status_code == 200, r.text


def test_auth_on_still_demands_a_bearer(client, monkeypatch) -> None:
    monkeypatch.setattr(me.settings, "auth_required", True)
    monkeypatch.setattr(me.settings, "identity_provider", "trust-network")
    assert client.get("/v1/me/service_accounts").status_code == 401
    r = client.post("/v1/me/service_accounts.revoke", json={"name": "x"})
    assert r.status_code == 401
