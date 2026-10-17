"""With auth off (single-user included), every route that saves a token and the
token list that shows and revokes it agree on who the caller is: a token minted
by `/v1/admin/token.mint` or the Setup page is listed on the Tokens page and
revoked from it, whether or not a bearer is sent."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
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


def _alice() -> dict[str, str]:
    token = mint_token(
        subject="alice", org="acme", namespaces=["home"], roles=["writer"], ttl_seconds=300
    )
    return {"Authorization": f"Bearer {token}"}


# Auth off ignores a bearer that does not verify: the caller is as if none were sent.
NO_CALLER = [{}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Basic eA=="}]


def _listed(client: TestClient, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    r = client.get("/v1/me/service_accounts", headers=headers)
    assert r.status_code == 200, r.text
    return {row["name"]: row for row in r.json()["rows"]}


def _revoke_and_check(client: TestClient, headers: dict[str, str], name: str) -> None:
    r = client.post("/v1/me/service_accounts.revoke", json={"name": name}, headers=headers)
    assert r.status_code == 200, r.text
    assert _listed(client, headers)[name]["revoked_at"] is not None


def _token_mint(client: TestClient, headers: dict[str, str], org: str) -> None:
    body = {"name": "ci-bot", "org": org, "namespaces": ["home"], "roles": ["viewer"]}
    r = client.post("/v1/admin/token.mint", json=body, headers=headers)
    assert r.status_code == 200, r.text


def _setup_mint(client: TestClient, headers: dict[str, str]) -> str:
    body = {"client": "cursor", "namespace": "home"}
    r = client.post("/v1/me/setup/mcp_config", json=body, headers=headers)
    assert r.status_code == 200, r.text
    [name] = _listed(client, headers)
    assert _listed(client, headers)[name]["kind"] == "mcp"
    return name


@pytest.mark.parametrize("headers", NO_CALLER)
def test_a_token_minted_by_token_mint_is_listed_and_revoked(
    client: TestClient, headers: dict[str, str]
) -> None:
    _token_mint(client, headers, "default")
    assert _listed(client, headers)["ci-bot"]["created_by"] == "dev"
    _revoke_and_check(client, headers, "ci-bot")


@pytest.mark.parametrize("headers", NO_CALLER)
def test_a_setup_page_token_is_listed_and_revoked(
    client: TestClient, headers: dict[str, str]
) -> None:
    _revoke_and_check(client, headers, _setup_mint(client, headers))


def test_a_bearers_token_mint_token_is_its_own_and_revoked_by_it(client: TestClient) -> None:
    _token_mint(client, _alice(), "acme")
    assert _listed(client, _alice())["ci-bot"]["created_by"] == "alice"
    assert _listed(client, {}) == {}
    _revoke_and_check(client, _alice(), "ci-bot")


def test_a_bearers_setup_page_token_is_its_own_and_revoked_by_it(client: TestClient) -> None:
    name = _setup_mint(client, _alice())
    assert _listed(client, _alice())[name]["created_by"] == "alice"
    assert _listed(client, {}) == {}
    _revoke_and_check(client, _alice(), name)
