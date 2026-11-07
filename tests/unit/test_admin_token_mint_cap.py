"""`POST /v1/admin/token.mint`: an org admin mints inside its own org, and the
token is delegated from the caller's (`server.auth.delegation`): every named
namespace needs the caller's live grant at or above the role asked for there,
org-wide roles are never minted, every mint leaves a `token.minted` audit row,
and the token is saved to the caller's token list under its own `sa:` subject."""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.jwt import mint_token
from server.identity.app import me_callers, token_grants
from server.memory.domain.trust import actor_for
from server.operations.base import Role
from server.settings import settings

ORG, PRIV = "clickhouse", "code-example-private"


def _bearer(roles: list[str], org: str = ORG, subject: str = "ops@x", ns=("*",)) -> dict:
    tok = mint_token(
        subject=subject,
        org=org,
        namespaces=list(ns),
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
    grants: dict = {(ORG, "clickgap-public", "ops@x"): Role.ADMIN}

    async def _grant(org, ns, subject):
        return grants.get((org, ns, subject))

    monkeypatch.setattr(authz, "grant_role_for", _grant)

    async def _role_on(org, ns, subject):
        role = grants.get((org, ns, subject))
        return role.name.lower() if role else None

    monkeypatch.setattr(me_callers, "caller_role_on", _role_on)
    saved: list = []

    class _CH:
        async def query_one(self, sql, params=None):
            if "max(version)" in sql:  # a new token's version, past its name's rows
                return {"v": time.time_ns()}
            return next(
                ({"created_by": r["created_by"]} for r in saved if r["name"] == params["name"]),
                None,
            )

        async def insert(self, table, rows, **kw):
            assert table == "loom.service_accounts"
            saved.extend(rows)

    restricted: dict = {}

    async def _grant_restricted(org, sa_subject, creator, ns_roles):
        restricted.update(ns_roles)
        return []

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(token_grants, "grant_restricted_to_token", _grant_restricted)
    app = FastAPI()
    app.include_router(admin_mod.org_admin_router)
    c = TestClient(app)
    c.grants = grants  # type: ignore[attr-defined]
    c.audit_rows = rows  # type: ignore[attr-defined]
    c.saved = saved  # type: ignore[attr-defined]
    c.restricted = restricted  # type: ignore[attr-defined]
    yield c
    nc.reset()


def _mint(client: TestClient, headers, namespaces, roles=None, name=None):
    # A name is one live token's; each mint here takes a fresh one.
    body = {"name": name or f"t-{uuid.uuid4().hex[:8]}", "org": ORG, "namespaces": namespaces}
    if roles is not None:
        body["roles"] = roles
    return client.post("/v1/admin/token.mint", json=body, headers=headers)


def test_a_namespace_the_caller_holds_mints(client) -> None:
    r = _mint(client, _bearer(["org_admin"]), ["clickgap-public"])
    assert r.status_code == 200 and r.json()["token"] == "jwt-minted"
    assert r.json()["ttl_seconds"] <= 300  # never outlives the caller's token
    assert [e for e, _ in client.audit_rows] == ["token.minted"]


def test_a_namespace_the_caller_holds_no_grant_on_is_refused(client) -> None:
    r = _mint(client, _bearer(["org_admin"]), ["someone-elses"])
    assert r.status_code == 403 and "someone-elses" in r.json()["detail"]["message"]
    assert client.audit_rows == []


def test_restricted_namespace_needs_the_callers_grant_at_the_role(client) -> None:
    r = _mint(client, _bearer(["org_admin"]), ["clickgap-public", PRIV])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
    client.grants[(ORG, PRIV, "ops@x")] = Role.VIEWER
    assert _mint(client, _bearer(["org_admin"]), [PRIV], roles=["writer"]).status_code == 403
    assert _mint(client, _bearer(["org_admin"]), [PRIV], roles=["viewer"]).status_code == 200


def test_a_mint_onto_a_restricted_namespace_writes_the_access_audit_row(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _grant_restricted(org, sa_subject, creator, ns_roles):
        return [ns for ns in ns_roles if ns == PRIV]

    monkeypatch.setattr(token_grants, "grant_restricted_to_token", _grant_restricted)
    monkeypatch.setattr(settings, "restricted_audit_inline", True)
    refused = _mint(client, _bearer(["org_admin"]), [PRIV], roles=["viewer"])
    assert refused.status_code == 403 and client.audit_rows == []  # no grant: no row
    client.grants[(ORG, PRIV, "ops@x")] = Role.VIEWER
    r = _mint(client, _bearer(["org_admin"]), ["clickgap-public", PRIV], roles=["viewer"])
    assert r.status_code == 200, r.text
    rows = [kw for e, kw in client.audit_rows if e == "namespace.restricted_access"]
    assert [(kw["namespace"], kw["subject"]["op"]) for kw in rows] == [(PRIV, "admin.token.mint")]
    assert rows[0]["subject"]["granted_to"] == r.json()["subject"]
    assert rows[0]["actor"] == actor_for("ops@x", is_service_account=False)


def test_a_restricted_namespace_beside_star_is_granted_at_the_callers_own_role(client) -> None:
    # An org admin who only views PRIV: the default writer is refused there, and a
    # viewer token's own grant on it is viewer.
    client.grants[(ORG, PRIV, "ops@x")] = Role.VIEWER
    named = ["clickgap-public", "*", PRIV]
    assert _mint(client, _bearer(["org_admin"]), named).status_code == 403
    r = _mint(client, _bearer(["org_admin"]), named, roles=["viewer"])
    assert r.status_code == 200, r.text
    assert client.restricted == {PRIV: "viewer"}


def test_other_org_is_refused_and_a_platform_admin_mints_only_in_its_own(client) -> None:
    assert _mint(client, _bearer(["org_admin"], org="acme"), ["clickgap-public"]).status_code == 403
    # a platform admin passes the gate for any org, but a child never changes org
    assert (
        _mint(client, _bearer(["platform_admin"], org="acme"), ["clickgap-public"]).status_code
        == 403
    )
    assert _mint(client, _bearer(["platform_admin"], org="*"), ["clickgap-public"]).status_code == (
        200
    )


def test_a_namespace_admin_cannot_reach_token_mint(client) -> None:
    assert _mint(client, _bearer(["admin"]), ["clickgap-public"]).status_code == 403


def test_wildcard_from_a_flat_caller_mints_a_dynamic_token(client) -> None:
    # a `*` child reads only where the caller holds a grant, rechecked per request
    assert _mint(client, _bearer(["org_admin"]), ["*"], roles=["viewer"]).status_code == 200


def test_ns_roles_mints_the_scoped_token_and_checks_each_namespace_on_its_own_role(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token that is admin on its own memory namespace and viewer on the
    restricted code namespace: each namespace is checked against its own
    role, not the flat max ("admin"), so the mint goes through."""
    from server.auth import jwt as jwt_mod

    seen: dict = {}

    def _mint_kw(**kw):
        seen.update(kw)
        return "jwt-scoped"

    monkeypatch.setattr(jwt_mod, "mint_token", _mint_kw)
    client.grants[(ORG, PRIV, "ops@x")] = Role.VIEWER
    client.grants[(ORG, "clickgap-private", "ops@x")] = Role.ADMIN
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["org_admin"]),
        json={
            "name": "clickgap",
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
    assert seen["ns_roles"] == {"clickgap-private": "admin", PRIV: "viewer"}
    assert seen["consumer"] == "clickgap" and seen["roles"] == []
    assert seen["creator"] == "ops@x" and len(seen["parents"]) == 1
    ((evt, kw),) = client.audit_rows
    assert evt == "token.minted" and kw["subject"]["ns_roles"][PRIV] == "viewer"


def test_ns_roles_asking_more_than_the_callers_grant_is_refused(client) -> None:
    client.grants[(ORG, PRIV, "ops@x")] = Role.VIEWER
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["org_admin"]),
        json={"name": "clickgap", "org": ORG, "ns_roles": {PRIV: "writer"}},
    )
    assert r.status_code == 403 and "writer" in r.json()["detail"]["message"]


def test_ns_roles_values_are_validated_and_namespaces_are_required_somewhere(client) -> None:
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["org_admin"]),
        json={"name": "s", "org": ORG, "ns_roles": {"n": "owner"}},
    )
    assert r.status_code == 422
    r = client.post(
        "/v1/admin/token.mint", headers=_bearer(["org_admin"]), json={"name": "s", "org": ORG}
    )
    assert r.status_code == 422


def test_flat_roles_mint_a_flat_token(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import jwt as jwt_mod

    seen: dict = {}
    monkeypatch.setattr(jwt_mod, "mint_token", lambda **kw: seen.update(kw) or "jwt-flat")
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["org_admin"]),
        json={"name": "s", "org": ORG, "namespaces": ["clickgap-public"], "roles": ["writer"]},
    )
    assert r.status_code == 200 and r.json()["ns_roles"] is None and r.json()["consumer"] == ""
    assert seen["ns_roles"] is None and seen["consumer"] is None and seen["roles"] == ["writer"]


def test_org_wide_roles_are_never_minted(client) -> None:
    for caller in (["org_admin"], ["platform_admin"]):
        for roles in (["org_admin"], ["platform_admin"], ["viewer", "org_admin"]):
            r = _mint(client, _bearer(caller), ["clickgap-public"], roles=roles)
            assert r.status_code == 403, (caller, roles)
    assert _mint(
        client, _bearer(["org_admin"]), ["clickgap-public"], roles=["nope"]
    ).status_code == (422)
    assert _mint(
        client, _bearer(["org_admin"]), ["clickgap-public"], roles=["admin"]
    ).status_code == (200)


def test_a_narrow_caller_mints_inside_its_own_claim(client) -> None:
    """A delegated caller (its own token names only team-a) cannot widen to `*`:
    the wildcard becomes its own list, and another namespace is refused."""
    narrow = {
        "Authorization": "Bearer "
        + mint_token(
            subject="ops@x",
            org=ORG,
            namespaces=["clickgap-public"],
            roles=["org_admin"],
            ttl_seconds=300,
            is_service_account=False,
            creator="ops@x",
        )
    }
    client.grants[(ORG, "team-b", "ops@x")] = Role.ADMIN
    r = _mint(client, narrow, ["*"], roles=["viewer"])
    assert r.status_code == 200 and r.json()["namespaces"] == ["clickgap-public"]
    assert _mint(client, narrow, ["team-b"], roles=["viewer"]).status_code == 403


def test_namespaces_beside_ns_roles_are_asked_for_as_viewer(client) -> None:
    client.grants[(ORG, PRIV, "ops@x")] = Role.VIEWER
    r = client.post(
        "/v1/admin/token.mint",
        headers=_bearer(["org_admin"]),
        json={
            "name": "clickgap",
            "org": ORG,
            "namespaces": [PRIV],
            "ns_roles": {"clickgap-public": "admin"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ns_roles"] == {PRIV: "viewer", "clickgap-public": "admin"}


def test_the_audit_row_records_what_was_granted_and_the_jti(client) -> None:
    r = _mint(client, _bearer(["org_admin"], ns=("clickgap-public",)), ["*"], roles=["admin"])
    assert r.status_code == 200, r.text
    ((_evt, kw),) = client.audit_rows
    audited = kw["subject"]
    assert audited["jti"] == r.json()["jti"]
    assert audited["namespaces"] == r.json()["namespaces"]
    assert audited["roles"] == r.json()["roles"]
    assert audited["ns_roles"] == r.json()["ns_roles"]


def test_the_token_is_saved_to_the_callers_list_under_its_own_subject(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import jwt as jwt_mod

    seen: dict = {}
    monkeypatch.setattr(jwt_mod, "mint_token", lambda **kw: seen.update(kw) or "jwt")
    r = _mint(client, _bearer(["org_admin"]), ["clickgap-public"], name="clickgap")
    assert r.status_code == 200, r.text
    ((row,),) = [client.saved]
    assert row["name"] == "clickgap" and row["created_by"] == "ops@x"
    assert row["sa_subject"].startswith("sa:clickgap-") and row["expires_at"] is not None
    assert seen["subject"] == row["sa_subject"] == r.json()["subject"]
    assert r.json()["name"] == "clickgap" and r.json()["ignored_args"] == []
    # Listed, and so revoked, on its own: the name is now taken.
    assert _mint(client, _bearer(["org_admin"]), ["x"], name="clickgap").status_code == 409


def test_a_subject_or_service_account_flag_sent_is_named_and_not_read(client) -> None:
    body = {
        "name": "clickgap",
        "subject": "alice",
        "is_service_account": False,
        "org": ORG,
        "namespaces": ["clickgap-public"],
    }
    r = client.post("/v1/admin/token.mint", json=body, headers=_bearer(["org_admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["ignored_args"] == ["is_service_account", "subject"]
    assert r.json()["subject"].startswith("sa:clickgap-")


def test_a_session_mints_what_was_asked_and_an_expiring_token_never_forever(client) -> None:
    session = {
        "Authorization": "Bearer "
        + mint_token(
            subject="ops@x",
            org=ORG,
            namespaces=["*"],
            roles=["org_admin"],
            ttl_seconds=3600,
            is_service_account=False,
            session_id="s1",
            first_party=True,
        )
    }
    body = {"name": "forever", "org": ORG, "namespaces": ["clickgap-public"], "ttl_seconds": 0}
    r = client.post("/v1/admin/token.mint", json=body, headers=session)
    assert r.status_code == 200 and r.json()["ttl_seconds"] is None
    body["name"] = "default"
    del body["ttl_seconds"]
    r = client.post("/v1/admin/token.mint", json=body, headers=session)
    assert r.json()["ttl_seconds"] == 90 * 86400
    body.update(name="expiring", ttl_seconds=0)
    r = client.post("/v1/admin/token.mint", json=body, headers=_bearer(["org_admin"]))
    assert r.status_code == 403


def test_a_mint_without_a_name_says_which_field_to_send(client) -> None:
    body = {"subject": "alice", "org": ORG, "namespaces": ["clickgap-public"]}
    r = client.post("/v1/admin/token.mint", json=body, headers=_bearer(["org_admin"]))
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "code": "VALIDATION_FAILED",
        "message": "`subject` is retired: pass `name` (the token gets its own `sa:` subject); "
        "`loom mint-token` mints for a given subject",
    }
    del body["subject"]
    r = client.post("/v1/admin/token.mint", json=body, headers=_bearer(["org_admin"]))
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "code": "VALIDATION_FAILED",
        "message": "name required, 1-64 chars",
    }
    assert client.saved == []


def test_a_name_holding_a_nul_is_a_422(client) -> None:
    r = _mint(client, _bearer(["org_admin"]), ["clickgap-public"], name="ci\x00bot")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "VALIDATION_FAILED"
    assert client.saved == []
