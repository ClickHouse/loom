"""`POST /v1/admin/token.mint`: mints inside the caller's org
only, and mints a restricted namespace only to a caller who holds a direct
grant there at or above the roles requested; a standard namespace is
unchanged; every mint leaves a `token.minted` audit row."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.jwt import mint_token
from server.settings import settings

ORG, PRIV = "clickhouse", "code-example-private"


def _bearer(roles: list[str], org: str = ORG, subject: str = "ops@x") -> dict[str, str]:
    tok = mint_token(
        subject=subject,
        org=org,
        namespaces=["*"],
        roles=roles,
        ttl_seconds=300,
        is_service_account=False,
    )
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", True)
    from server.auth import jwt as jwt_mod
    from server.insights.app import admin_routes as admin_mod

    monkeypatch.setattr(jwt_mod, "mint_token", lambda **kw: "jwt-minted")
    rows: list = []

    async def _emit(event_type, **kw):
        rows.append((event_type, kw))
        return "h"

    monkeypatch.setattr(admin_mod.audit, "emit", _emit)
    nc.reset()
    nc._cache[ORG] = (1e12, frozenset({PRIV}))
    grants: dict = {}

    async def _grant(org, ns, subject):
        return grants.get((org, ns, subject))

    monkeypatch.setattr(authz, "_grant_role_for", _grant)
    app = FastAPI()
    app.include_router(admin_mod.admin_router)
    c = TestClient(app)
    c.grants = grants  # type: ignore[attr-defined]
    c.audit_rows = rows  # type: ignore[attr-defined]
    yield c
    nc.reset()


def _mint(client: TestClient, headers, namespaces, roles=None):
    body = {"subject": "sa:clickgap", "org": ORG, "namespaces": namespaces}
    if roles is not None:
        body["roles"] = roles
    return client.post("/v1/admin/token.mint", json=body, headers=headers)


def test_standard_namespace_mints_as_before(client) -> None:
    r = _mint(client, _bearer(["admin"]), ["clickgap-public"])
    assert r.status_code == 200 and r.json()["token"] == "jwt-minted"
    assert [e for e, _ in client.audit_rows] == ["token.minted"]


def test_restricted_namespace_needs_the_callers_direct_grant(client) -> None:
    r = _mint(client, _bearer(["admin"]), ["clickgap-public", PRIV])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
    assert client.audit_rows == []
    from server.operations.base import Role

    client.grants[(ORG, PRIV, "ops@x")] = Role.VIEWER
    assert _mint(client, _bearer(["admin"]), [PRIV], roles=["writer"]).status_code == 403
    assert _mint(client, _bearer(["admin"]), [PRIV], roles=["viewer"]).status_code == 200


def test_other_org_is_refused_unless_platform_admin(client) -> None:
    r = _mint(client, _bearer(["admin"], org="acme"), ["clickgap-public"])
    assert r.status_code == 403
    r = _mint(client, _bearer(["platform_admin"], org="acme"), ["clickgap-public"])
    assert r.status_code == 200


def test_wildcard_namespace_is_not_a_restricted_mint(client) -> None:
    # a `*` token never reads a restricted namespace, so it needs no grant
    assert _mint(client, _bearer(["admin"]), ["*"]).status_code == 200


# ------------------------------------------------------- ns_roles + consumer


def test_ns_roles_mints_the_scoped_token_and_checks_each_namespace_on_its_own_role(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token that is admin on its own memory namespace and viewer on the
    restricted code namespace: each namespace is checked against its own
    role, not the flat max ("admin"), so the mint goes through."""
    from server.auth import jwt as jwt_mod
    from server.operations.base import Role

    seen: dict = {}

    def _mint(**kw):
        seen.update(kw)
        return "jwt-scoped"

    monkeypatch.setattr(jwt_mod, "mint_token", _mint)
    client.grants[(ORG, PRIV, "ops@x")] = (
        Role.VIEWER
    )  # the caller holds viewer on the restricted ns
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["admin"]),
        json={
            "subject": "sa:clickgap",
            "org": ORG,
            "ns_roles": {"clickgap-private": "admin", PRIV: "viewer"},
            "consumer": "clickgap",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] == "jwt-scoped" and body["consumer"] == "clickgap"
    assert body["ns_roles"] == {"clickgap-private": "admin", PRIV: "viewer"}
    assert body["namespaces"] == ["clickgap-private", PRIV]  # derived from the map, in order
    assert (
        seen["ns_roles"] == {"clickgap-private": "admin", PRIV: "viewer"}
        and seen["consumer"] == "clickgap"
    )
    ((evt, kw),) = client.audit_rows
    assert (
        evt == "token.minted"
        and kw["subject"]["ns_roles"][PRIV] == "viewer"
        and kw["subject"]["consumer"] == "clickgap"
    )


def test_ns_roles_asking_more_than_the_callers_grant_on_a_restricted_namespace_is_refused(
    client,
) -> None:
    from server.operations.base import Role

    client.grants[(ORG, PRIV, "ops@x")] = Role.VIEWER
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["admin"]),
        json={"subject": "sa:clickgap", "org": ORG, "ns_roles": {PRIV: "writer"}},
    )
    assert r.status_code == 403 and "writer" in r.json()["detail"]["message"]


def test_ns_roles_values_are_validated_and_namespaces_are_required_somewhere(client) -> None:
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["admin"]),
        json={"subject": "s", "org": ORG, "ns_roles": {"n": "owner"}},
    )
    assert r.status_code == 422
    r = client.post(
        "/v1/admin/token.mint", headers=_bearer(["admin"]), json={"subject": "s", "org": ORG}
    )
    assert r.status_code == 422


def test_flat_roles_still_mint_as_before(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import jwt as jwt_mod

    seen: dict = {}
    monkeypatch.setattr(jwt_mod, "mint_token", lambda **kw: seen.update(kw) or "jwt-flat")
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["admin"]),
        json={"subject": "s", "org": ORG, "namespaces": ["bench"], "roles": ["writer"]},
    )
    assert r.status_code == 200 and r.json()["ns_roles"] is None and r.json()["consumer"] == ""
    assert seen["ns_roles"] is None and seen["consumer"] is None


def test_a_minted_token_never_outranks_its_minter(client) -> None:
    admin = _bearer(["admin"])
    for roles in (["org_admin"], ["platform_admin"], ["viewer", "org_admin"]):
        r = _mint(client, admin, ["clickgap-public"], roles=roles)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "AUTH_INSUFFICIENT", roles
    assert _mint(client, admin, ["clickgap-public"], roles=["nope"]).status_code == 422
    assert _mint(client, admin, ["clickgap-public"], roles=["admin"]).status_code == 200
    assert _mint(client, _bearer(["org_admin"]), ["x"], roles=["org_admin"]).status_code == 200
    assert client.audit_rows and all(e == "token.minted" for e, _ in client.audit_rows)


def test_an_org_wide_read_is_minted_only_by_a_caller_holding_one(client) -> None:
    narrow = {
        "Authorization": "Bearer "
        + mint_token(
            subject="ops@x",
            org=ORG,
            namespaces=["team-a"],
            roles=["admin"],
            ttl_seconds=300,
            is_service_account=False,
        )
    }
    assert _mint(client, narrow, ["*"], roles=["viewer"]).status_code == 403
    assert _mint(client, narrow, ["team-a"], roles=["viewer"]).status_code == 200
    assert _mint(client, _bearer(["admin"]), ["*"], roles=["viewer"]).status_code == 200
    assert _mint(client, _bearer(["org_admin"]), ["*"], roles=["viewer"]).status_code == 200
