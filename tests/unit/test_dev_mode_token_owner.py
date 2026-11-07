"""With auth off (single-user included), every route that saves a token and the
token list that shows and revokes it agree on who the caller is: a token minted
by `/v1/admin/token.mint` or the Setup page is listed on the Tokens page and
revoked from it, whether or not a bearer is sent."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
from server.operations.base import Role
from server.settings import settings
from tests.unit.revocation_fakes import revocation_read


class _Store:
    """`loom.service_accounts` as ReplacingMergeTree FINAL reads it: the last row per name."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.revocations: list[dict[str, Any]] = []  # loom.revoked_tokens

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        if table == "loom.revoked_tokens":
            self.revocations.extend(rows)
        if table == "loom.service_accounts":
            for r in rows:
                self.rows[(r["org"], r["name"])] = {"revoked_at": None, "revoked_by": "", **r}

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        if "loom.revoked_tokens" in sql:
            return revocation_read(self.revocations, sql, params)
        if "max(version)" in sql:  # a new token's version, past its name's rows
            return {"v": time.time_ns()}
        row = self.rows.get((params["org"], params["name"]))
        if row is None or ("revoked_at IS NULL" in sql and row["revoked_at"] is not None):
            return None
        return row

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        return [
            r
            for (org, _name), r in self.rows.items()
            if org == params["org"] and r["created_by"].lower() in params["subs"]
        ]


@pytest.fixture(params=["auth-off", "single-user"])
def client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from server.auth import authz, revocation
    from server.identity.app import me_callers, me_service_accounts, token_grants
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

    async def _aliases(org: str, subject: str) -> list[str]:
        # alice signs in as `alice` or under her email: one person.
        if subject in ("alice", "alice@acme.dev"):
            return ["alice", "alice@acme.dev"]
        return [subject]

    async def _none(*a: Any, **k: Any) -> Any:
        return None

    async def _writer(*a: Any, **k: Any) -> Role:
        return Role.WRITER

    async def _zero(*a: Any, **k: Any) -> int:
        return 0

    async def _no_grants(*a: Any, **k: Any) -> list[str]:
        return []

    import server.core.grant_cache as grant_cache
    from server.auth import delegation
    from server.identity.app import caller_defaults, namespace_classification

    # alice's live grants, the same to the grant cache and to the default's ranking.
    alice = ["personal-alice", "team-x"]

    async def _grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks=dict.fromkeys(alice, 2) if subject == "alice" else {})

    async def _ranked(org: str, subs: list[str], **_: Any) -> list[dict[str, str]]:
        return [{"namespace": ns, "role": "writer"} for ns in alice] if "alice" in subs else []

    async def _active(*_a: Any, **_k: Any) -> bool:
        return True

    async def _none_restricted(org: str) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(delegation, "creator_account_active", _active)
    monkeypatch.setattr(namespace_classification, "restricted_set", _none_restricted)
    monkeypatch.setattr(grant_cache, "_read", _grant_map)

    async def _no_pick(*_a: Any, **_k: Any) -> str:
        return ""

    monkeypatch.setattr(caller_defaults, "ranked_grants", _ranked)
    monkeypatch.setattr(caller_defaults, "saved_default", _no_pick)
    monkeypatch.setattr(authz, "grant_role_for", _writer)
    monkeypatch.setattr(me_callers, "subject_aliases_of", _aliases)
    monkeypatch.setattr(me_callers, "caller_admin_of", _none)
    monkeypatch.setattr(revocation, "refresh", _zero)
    monkeypatch.setattr(token_grants, "grant_restricted_to_token", _no_grants)
    monkeypatch.setattr(token_grants, "revoke_token_grants", _zero)

    app = FastAPI()
    app.include_router(admin_routes.org_admin_router)
    app.include_router(me_service_accounts.router)
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


def _mint_default_token(client: TestClient, headers: dict[str, str]) -> str:
    r = client.post("/v1/me/service_accounts.create", json={}, headers=headers)
    assert r.status_code == 200, r.text
    [name] = _listed(client, headers)
    assert name == r.json()["name"] and name.startswith("token-")
    return name


@pytest.mark.parametrize("headers", NO_CALLER)
def test_a_token_minted_by_token_mint_is_listed_and_revoked(
    client: TestClient, headers: dict[str, str]
) -> None:
    _token_mint(client, headers, "default")
    assert _listed(client, headers)["ci-bot"]["created_by"] == "dev"
    _revoke_and_check(client, headers, "ci-bot")


@pytest.mark.parametrize("headers", NO_CALLER)
def test_a_token_made_without_a_name_is_listed_and_revoked(
    client: TestClient, headers: dict[str, str]
) -> None:
    _revoke_and_check(client, headers, _mint_default_token(client, headers))


def test_a_wildcard_token_names_the_default_namespace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A `*` token with auth off writes to the default namespace, not alice's `home`.
    monkeypatch.setattr(settings, "default_namespace", "shared-dev")
    r = client.post("/v1/me/service_accounts.create", json={}, headers=_alice())
    assert r.status_code == 200, r.text
    assert r.json()["saves_to"] == "shared-dev"


def test_a_wildcard_token_with_auth_off_outside_single_user_names_the_personal_namespace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only single-user writes to the default namespace; MCP falls back to personal-<sub>.
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")
    monkeypatch.setattr(settings, "default_namespace", "shared-dev")
    r = client.post("/v1/me/service_accounts.create", json={}, headers=_alice())
    assert r.status_code == 200, r.text
    assert r.json()["saves_to"] == "personal-alice"


def test_a_wildcard_token_from_a_wildcard_bearer_names_the_personal_namespace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "default_namespace", "shared-dev")
    token = mint_token(
        subject="alice", org="acme", namespaces=["*"], roles=["writer"], ttl_seconds=300
    )
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post("/v1/me/service_accounts.create", json={}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["saves_to"] == "personal-alice"


def test_a_wildcard_token_names_where_its_unaddressed_calls_land(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Alice's saved switcher pick is a team she writes: MCP lands there, so the page says so.
    from server.auth import aliases
    from server.identity.app import caller_defaults

    async def _saved(org: str, subject: str, **_: Any) -> str:
        return "team-x" if subject == "alice" else ""

    async def _own(org: str, subject: str, **_: Any) -> list[str]:
        return [subject]

    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")
    monkeypatch.setattr(caller_defaults, "saved_default", _saved)
    monkeypatch.setattr(aliases, "subject_aliases", _own)
    r = client.post("/v1/me/service_accounts.create", json={}, headers=_alice())
    assert r.status_code == 200, r.text
    assert r.json()["saves_to"] == "team-x"


def test_a_bearers_token_mint_token_is_its_own_and_revoked_by_it(client: TestClient) -> None:
    _token_mint(client, _alice(), "acme")
    assert _listed(client, _alice())["ci-bot"]["created_by"] == "alice"
    assert _listed(client, {}) == {}
    _revoke_and_check(client, _alice(), "ci-bot")


def test_a_bearers_unnamed_token_is_its_own_and_revoked_by_it(client: TestClient) -> None:
    name = _mint_default_token(client, _alice())
    assert _listed(client, _alice())[name]["created_by"] == "alice"
    assert _listed(client, _alice())[name]["yours"] is True
    assert _listed(client, {}) == {}
    _revoke_and_check(client, _alice(), name)


def test_a_token_is_listed_and_revoked_under_another_spelling_of_its_maker(
    client: TestClient,
) -> None:
    name = _mint_default_token(client, _alice())
    email = mint_token(
        subject="alice@acme.dev", org="acme", namespaces=["home"], roles=["writer"], ttl_seconds=300
    )
    as_email = {"Authorization": f"Bearer {email}"}
    assert _listed(client, as_email)[name]["yours"] is True
    _revoke_and_check(client, as_email, name)


def test_a_token_is_its_makers_whatever_the_case_of_the_spelling(client: TestClient) -> None:
    upper = mint_token(
        subject="Alice", org="acme", namespaces=["home"], roles=["writer"], ttl_seconds=300
    )
    name = _mint_default_token(client, {"Authorization": f"Bearer {upper}"})
    assert _listed(client, _alice())[name]["yours"] is True
    _revoke_and_check(client, _alice(), name)


def _create(client: TestClient, headers: dict[str, str], **body: Any) -> Any:
    return client.post(
        "/v1/me/service_accounts.create", json={"name": "agent", **body}, headers=headers
    )


def test_a_bearer_names_the_owner_but_does_not_cap_the_token(client: TestClient) -> None:
    # Alice's bearer lives 300 s and holds `home` only; with auth off it names
    # the owner and nothing else. (A per-namespace token never names "*".)
    r = _create(client, _alice(), namespaces=["home", "team"], ttl_days=90)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["org"] == "acme"
    assert out["namespaces"] == ["home", "team"]
    lifetime = datetime.fromisoformat(out["expires_at"]) - datetime.fromisoformat(out["created_at"])
    assert lifetime == timedelta(days=90)
    assert _listed(client, _alice())["agent"]["created_by"] == "alice"


def test_an_unavailable_revocation_list_answers_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import bearer
    from server.auth import jwt as jwt_mod

    def _unavailable(token: str) -> Any:
        raise jwt_mod.RevocationUnavailable("revocation list not loaded")

    monkeypatch.setattr(jwt_mod, "verify_token", _unavailable)
    monkeypatch.setattr(bearer, "verify_token", _unavailable)
    r = _create(client, _alice(), namespaces=["home"])
    assert r.status_code == 503, r.text
    assert r.headers["Retry-After"] == "5"
    assert r.json()["detail"]["code"] == "AUTH_UNAVAILABLE"


def test_a_bearer_cannot_mint_into_another_org(client: TestClient) -> None:
    body = {"name": "ci-bot", "org": "default", "namespaces": ["home"], "roles": ["viewer"]}
    r = client.post("/v1/admin/token.mint", json=body, headers=_alice())
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"


def test_the_retired_subject_does_not_name_the_owner(client: TestClient) -> None:
    body = {
        "name": "ci-bot",
        "org": "acme",
        "subject": "bob",
        "namespaces": ["home"],
        "roles": ["viewer"],
    }
    r = client.post("/v1/admin/token.mint", json=body)
    assert r.status_code == 200, r.text
    assert "subject" in r.json()["ignored_args"]
    rows = client.get("/v1/me/service_accounts", params={"org": "acme"}).json()["rows"]
    assert [(row["name"], row["created_by"]) for row in rows] == [("ci-bot", "dev")]


def test_auth_on_still_delegates_from_the_bearer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    assert settings.auth_actually_required()
    r = _create(client, _alice(), namespaces=["home"], ttl_days=90)
    assert r.status_code == 200, r.text
    out = r.json()
    lifetime = datetime.fromisoformat(out["expires_at"]) - datetime.fromisoformat(out["created_at"])
    assert lifetime <= timedelta(seconds=300)
    body = {"name": "ci-bot", "org": "acme", "namespaces": ["home"], "roles": ["viewer"]}
    assert client.post("/v1/admin/token.mint", json=body, headers=_alice()).status_code == 403
    assert client.post("/v1/admin/token.mint", json=body).status_code == 401
