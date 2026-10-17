"""With auth off (single-user included), every route that saves a token and the
token list that shows and revokes it agree on who the caller is: a token minted
by `/v1/admin/token.mint` or the Setup page is listed on the Tokens page and
revoked from it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.operations.base import Role
from server.settings import settings


class _Store:
    """`loom.service_accounts` as ReplacingMergeTree FINAL reads it: the last row per name."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        if table == "loom.service_accounts":
            for r in rows:
                self.rows[(r["org"], r["name"])] = {"revoked_at": None, "revoked_by": "", **r}

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        row = self.rows.get((params["org"], params["name"]))
        if row is None or ("revoked_at IS NULL" in sql and row["revoked_at"] is not None):
            return None
        return row

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        return [
            r
            for (org, _name), r in self.rows.items()
            if org == params["org"] and r["created_by"] == params["sub"]
        ]


@pytest.fixture(params=["auth-off", "single-user"])
def client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from server.auth import authz, revocation
    from server.auth import jwt as jwt_mod
    from server.identity.app import me_callers, me_service_accounts, me_setup, token_grants
    from server.insights.app import admin_routes

    if request.param == "single-user":
        # Single-user overrides auth_required=True: no bearer is checked.
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "identity_provider", "single-user")
    else:
        monkeypatch.setattr(settings, "auth_required", False)
    assert not settings.auth_actually_required()

    store = _Store()
    monkeypatch.setattr("server.core.ch.get_client", lambda: store)
    monkeypatch.setattr(jwt_mod, "mint_token", lambda **kw: "jwt-minted")

    async def _aliases(org: str, subject: str) -> list[str]:
        return [subject]

    async def _none(*a: Any, **k: Any) -> Any:
        return None

    async def _writer(*a: Any, **k: Any) -> Role:
        return Role.WRITER

    async def _zero(*a: Any, **k: Any) -> int:
        return 0

    async def _no_grants(*a: Any, **k: Any) -> list[str]:
        return []

    monkeypatch.setattr(authz, "grant_role_for", _writer)
    monkeypatch.setattr(me_callers, "subject_aliases_of", _aliases)
    monkeypatch.setattr(me_callers, "caller_admin_of", _none)
    monkeypatch.setattr(revocation, "refresh", _zero)
    monkeypatch.setattr(token_grants, "grant_restricted_to_token", _no_grants)
    monkeypatch.setattr(token_grants, "revoke_token_grants", _zero)

    app = FastAPI()
    app.include_router(admin_routes.org_admin_router)
    app.include_router(me_service_accounts.router)
    app.include_router(me_setup.router)
    return TestClient(app)


def _listed(client: TestClient) -> dict[str, dict[str, Any]]:
    r = client.get("/v1/me/service_accounts")
    assert r.status_code == 200, r.text
    return {row["name"]: row for row in r.json()["rows"]}


def _revoke_and_check(client: TestClient, name: str) -> None:
    r = client.post("/v1/me/service_accounts.revoke", json={"name": name})
    assert r.status_code == 200, r.text
    assert _listed(client)[name]["revoked_at"] is not None


def test_a_token_minted_by_token_mint_is_listed_and_revoked(client: TestClient) -> None:
    r = client.post(
        "/v1/admin/token.mint",
        json={"name": "ci-bot", "org": "default", "namespaces": ["home"], "roles": ["viewer"]},
    )
    assert r.status_code == 200, r.text
    assert "ci-bot" in _listed(client)
    _revoke_and_check(client, "ci-bot")


def test_a_setup_page_token_is_listed_and_revoked(client: TestClient) -> None:
    r = client.post("/v1/me/setup/mcp_config", json={"client": "cursor", "namespace": "home"})
    assert r.status_code == 200, r.text
    [name] = _listed(client)
    assert _listed(client)[name]["kind"] == "mcp"
    _revoke_and_check(client, name)
