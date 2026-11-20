"""Actual JWT and registered route admissions enforce the complete person graph."""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from server.auth import authz, bearer, jwt, keyring, revocation, routes, scim, tenancy
from server.core import cell
from server.identity.app import me_callers, me_routes, me_service_accounts
from server.operations.base import Role
from server.settings import settings
from tests.unit.test_scim_people import people as people

SECRET = "unit-person-provisioning-signature-" * 3


@pytest.fixture
def token(people, monkeypatch):
    monkeypatch.setattr(cell, "gate_reason", lambda: "")
    monkeypatch.setattr(cell, "first_identity", lambda: (True, "first"))
    monkeypatch.setattr(keyring, "loaded", lambda: True)
    monkeypatch.setattr(keyring, "stale", lambda: False)
    monkeypatch.setattr(keyring, "verification_keys", lambda *_: [SECRET])
    monkeypatch.setattr(revocation, "loaded", lambda: True)
    monkeypatch.setattr(revocation, "snapshot_age_seconds", lambda: 0)
    monkeypatch.setattr(revocation, "is_revoked", lambda *_, **__: False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(settings, "self_signup_org", "acme")

    def mint(subject="alice@example.test", *, org="acme", parents=(), creator=None, sa=False):
        return jwt.mint_token(
            subject,
            org,
            ["*"],
            ["platform_admin"],
            secret=SECRET,
            is_service_account=sa,
            parent_subjects=list(parents),
            parent_iats=[int(time.time())] * len(parents),
            creator=creator,
            ttl_seconds=60,
        )

    return mint


def request_for(raw):
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {raw}".encode())]})


async def test_raw_crypto_remains_independent_but_admission_requires_positive_provisioning(token):
    raw = token()
    assert jwt.verify_token(raw, secret=SECRET).subject == "alice@example.test"
    with pytest.raises(jwt.TokenError):
        await jwt.authenticate_token(raw, secret=SECRET)


@pytest.mark.parametrize("sa", [False, True])
async def test_a_service_account_claim_cannot_exempt_a_person(token, sa):
    with pytest.raises(jwt.TokenError):
        await jwt.authenticate_token(token(sa=sa), secret=SECRET)


@pytest.mark.parametrize("field", ["creator", "parents"])
async def test_creator_and_every_parent_belong_to_one_admission(people, token, field):
    people.provision("alice@example.test")
    kwargs = {field: ("missing@example.test",) if field == "parents" else "missing@example.test"}
    with pytest.raises(jwt.TokenError):
        await jwt.authenticate_token(token(**kwargs), secret=SECRET)


async def test_signed_gateway_root_overflow_refuses_before_any_identity_read(people, token):
    people.provision("alice@example.test")
    raw = token(parents=("alice@example.test",) * 1024)
    assert len(jwt.verify_token(raw, secret=SECRET).parent_subjects) == 1024
    with pytest.raises(jwt.TokenError):
        await jwt.authenticate_token(raw, secret=SECRET)
    assert not people.reads


async def test_exact_root_limit_passes_after_counting_duplicates(people, token):
    people.provision("alice@example.test")
    admitted = await jwt.authenticate_token(
        token(parents=("alice@example.test",) * 1023), secret=SECRET
    )
    assert admitted.subject == "alice@example.test"


@pytest.mark.parametrize("active,refused", [(True, False), (False, True)])
async def test_only_a_registered_active_bot_has_the_closed_exemption(
    people, token, active, refused
):
    people.bots["robot"] = active
    if refused:
        with pytest.raises(jwt.TokenError):
            await jwt.authenticate_token(token("sa:robot:agent"), secret=SECRET)
    else:
        assert (
            await jwt.authenticate_token(token("sa:robot:agent"), secret=SECRET)
        ).subject == "sa:robot:agent"


async def test_a_real_personal_service_account_uses_its_creator(people, token):
    people.provision("alice@example.test")
    people.services["sa:personal:one"] = {"created_by": "alice@example.test", "lineage": []}
    assert (
        await jwt.authenticate_token(token("sa:personal:one"), secret=SECRET)
    ).subject == "sa:personal:one"


@pytest.mark.parametrize("required", [False, True])
async def test_revoked_personal_credential_cannot_be_replaced_by_impersonation(
    people, monkeypatch, required
):
    from server.auth import gateway
    from server.core import grant_cache

    subject = "sa:personal:one"
    people.provision("alice@example.test")
    people.services[subject] = {"created_by": "alice@example.test", "lineage": []}
    people.bots["gateway"] = True
    if not required:
        monkeypatch.setattr(settings, "scim_token", "")
    monkeypatch.setattr(settings, "trusted_callers", "sa:gateway")
    rows = []
    identity_read = people.query_one

    async def query_one(sql, params=None):
        if "loom.revoked_tokens" in sql:
            assert params == {"k0": credential.token_id}
            return {"v": 1}
        return await identity_read(sql, params)

    async def insert(table, written):
        assert table == "loom.revoked_tokens"
        rows.extend(written)

    async def grant_map(org, held):
        assert org == "acme"
        return grant_cache.GrantMap(ranks={}, held=(held,))

    monkeypatch.setattr(people, "query_one", query_one)
    monkeypatch.setattr(people, "insert", insert, raising=False)
    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    raw = jwt.mint_token(
        subject,
        "acme",
        ["*"],
        ["writer"],
        secret=SECRET,
        is_service_account=True,
        creator="alice@example.test",
        ttl_seconds=60,
    )
    credential = await jwt.authenticate_token(raw, secret=SECRET)
    await authz.check_acted_for(credential, "acme")
    live_row = dict(people.services[subject])
    await revocation.revoke(credential.token_id, reason="end the personal credential")
    assert [row["jti"] for row in rows] == [credential.token_id]
    assert revocation.is_revoked(credential.token_id)
    assert not revocation.is_revoked(subject)
    with pytest.raises(jwt.TokenError, match="revoked"):
        jwt.verify_token(raw, secret=SECRET)
    assert people.services[subject] == live_row
    assert live_row.get("revoked_at") is live_row.get("expires_at") is None

    gateway_raw = jwt.mint_token(
        "sa:gateway",
        "acme",
        ["*"],
        ["writer"],
        secret=SECRET,
        is_service_account=True,
        ttl_seconds=60,
    )
    caller = await jwt.authenticate_token(gateway_raw, secret=SECRET)
    effective = gateway.effective_auth(caller, subject)
    assert effective.subject == subject
    assert effective.token_id == caller.token_id != credential.token_id
    assert effective.impersonated_by == caller.subject
    with pytest.raises(authz.ActedForRefused, match="active bot"):
        await authz.check_acted_for(effective, "acme")


async def test_admission_outage_is_retryable_auth_unavailability(people, token):
    people.unavailable = True
    with pytest.raises(jwt.AuthUnavailable):
        await jwt.authenticate_token(token(), secret=SECRET)


def test_registered_whoami_refuses_an_unprovisioned_person(token):
    app = FastAPI()
    routes.attach(app)
    response = TestClient(app).get("/v1/whoami", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_registered_whoami_reports_an_outage_as_retryable(people, token):
    people.unavailable = True
    app = FastAPI()
    routes.attach(app)
    response = TestClient(app).get("/v1/whoami", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"


@pytest.mark.parametrize("auth_required", [False, True])
def test_real_nonop_caller_route_refuses_before_writing(token, monkeypatch, auth_required):
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: auth_required)

    async def no_write(*_, **__):
        pytest.fail("an unchecked person must not write the default namespace")

    from server.core.ch import get_client

    monkeypatch.setattr(get_client(), "insert", no_write, raising=False)
    app = FastAPI()
    me_routes.attach(app)
    response = TestClient(app).post(
        "/v1/me/set-default-namespace",
        json={"namespace": "private"},
        headers={"Authorization": f"Bearer {token()}"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("which", ["caller", "service_account"])
async def test_actual_bearer_refusal_cannot_become_a_dev_identity(token, monkeypatch, which):
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    with pytest.raises(HTTPException) as refused:
        if which == "caller":
            await me_callers.caller_identity(request_for(token()))
        else:
            await me_service_accounts.dev_caller(request_for(token()), {})
    assert refused.value.status_code == 401


async def test_concrete_authorization_rechecks_the_full_wildcard_graph(people, token, monkeypatch):
    people.provision("alice@example.test")
    auth = await jwt.authenticate_token(token(org="*"), secret=SECRET)
    people.accounts.append(("acme", "alice@example.test", "missing@example.test", False))

    async def no_placement(*_, **__):
        pytest.fail("provisioning refusal must precede tenancy placement")

    monkeypatch.setattr(tenancy, "ensure_held", no_placement)
    with pytest.raises(PermissionError):
        await authz.check_authorized(auth, Role.VIEWER, "acme", "private")


async def test_acting_for_a_person_retains_the_real_bearer_root(people, token, monkeypatch):
    from server.auth.gateway import effective_auth

    for email in ("alice@example.test", "target@example.test"):
        people.provision(email)
    auth = await jwt.authenticate_token(token(org="*"), secret=SECRET)
    monkeypatch.setattr(settings, "trusted_callers", "alice@example.test")
    effective = effective_auth(auth, "target@example.test")
    people.accounts.append(("acme", "alice@example.test", "missing@example.test", False))
    with pytest.raises(authz.ActedForRefused):
        await authz.check_acted_for(effective, "acme")


async def test_bearer_header_chain_maps_missing_provisioning_to_401(token):
    with pytest.raises(HTTPException) as refused:
        await bearer.verify_bearer_header(f"Bearer {token()}")
    assert refused.value.status_code == 401


@pytest.mark.parametrize("outage,status", [(False, 401), (True, 503)])
def test_registered_scim_groups_jwt_fallback_enforces_people(
    people, token, monkeypatch, outage, status
):
    from server.identity.app import scim_groups

    admitted = []
    real_decision = scim.refuses_subject

    async def decision(*args, **kwargs):
        admitted.append((args, kwargs))
        return await real_decision(*args, **kwargs)

    real_query = people.query_one

    async def query(sql, params=None):
        if "loom.principal_groups" in sql:
            pytest.fail("a refused person must not read groups")
        return await real_query(sql, params=params)

    monkeypatch.setattr(scim, "refuses_subject", decision)
    monkeypatch.setattr(people, "query_one", query)
    people.unavailable = outage
    app = FastAPI()
    scim_groups.attach(app)
    response = TestClient(app).get(
        "/scim/v2/Groups", headers={"Authorization": f"Bearer {token()}"}
    )
    assert response.status_code == status
    assert len(admitted) == 1
    assert admitted[0][0] == ("alice@example.test",)


@pytest.mark.parametrize("method", ["post", "delete"])
@pytest.mark.parametrize("mode", ["required", "open"])
@pytest.mark.parametrize("outage,status", [(False, 401), (True, 503)])
def test_registered_mcp_cannot_downgrade_provisioning_failure_to_anonymous(
    people, token, monkeypatch, mode, outage, status, method
):
    from server.transport import mcp

    monkeypatch.setattr(settings, "mcp_auth_mode", mode)
    people.unavailable = outage
    app = FastAPI()
    mcp.attach(app)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token()}"}
    if method == "post":
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    else:
        response = client.delete("/mcp", headers=headers)
    assert response.status_code == status
    if outage:
        assert response.headers["Retry-After"] == "5"


async def test_health_admin_detail_requires_full_person_admission(token):
    from server import health

    assert not await health._is_platform_admin(request_for(token()))


def test_tenancy_middleware_rechecks_a_wildcard_before_placement(people, token, monkeypatch):
    from server.transport.tenancy_gate import TenancyMiddleware

    people.provision("alice@example.test")
    people.accounts.append(("acme", "alice@example.test", "missing@example.test", False))

    async def no_placement(*_, **__):
        pytest.fail("an unchecked person must not place an org")

    monkeypatch.setattr(tenancy, "ensure_held", no_placement)
    app = FastAPI()
    me_routes.attach(app)
    app.add_middleware(TenancyMiddleware)
    response = TestClient(app).post(
        "/v1/me/set-default-namespace",
        json={"namespace": "private"},
        headers={"Authorization": f"Bearer {token(org='*')}"},
    )
    assert response.status_code == 401


def test_registered_http_operation_refuses_before_dispatch(people, token, monkeypatch):
    from dataclasses import replace

    from server.transport import http

    called = []

    async def no_dispatch(*_, **__):
        called.append(True)
        pytest.fail("an unprovisioned token must not reach operation dispatch")

    operation = next(op for op in http.ALL_OPERATIONS if op.name == "memory.get")

    async def no_placement(*_, **__):
        pytest.fail("an unchecked HTTP person must not place an org")

    monkeypatch.setattr(http, "ALL_OPERATIONS", [replace(operation, handler=no_dispatch)])
    monkeypatch.setattr(tenancy, "ensure_held", no_placement)
    app = FastAPI()
    app.include_router(http.build_router())
    payload = {"org": "acme", "namespace": "private", "agent": "agent", "memory_key": "key"}
    operation.input_model.model_validate(payload)
    response = TestClient(app).post(
        "/v1/memory.get", json=payload, headers={"Authorization": f"Bearer {token()}"}
    )
    assert response.status_code == 401
    assert not called


async def test_actual_signed_jwt_disjoint_graphs_share_one_subject_budget(people, token):
    roots = []
    for graph in range(65):
        email = f"p{graph}@example.test"
        people.provision(email)
        names = [f"sa:personal:{graph}-{node}" for node in range(16)]
        roots.append(names[0])
        for current, parent in zip(names, [*names[1:], email], strict=True):
            people.services[current] = {"created_by": parent, "lineage": []}
    with pytest.raises(jwt.TokenError):
        await jwt.authenticate_token(token(roots[0], parents=roots[1:]), secret=SECRET)


async def test_actual_signed_jwt_root_bytes_refuse_before_store_reads(people, token):
    with pytest.raises(jwt.TokenError):
        await jwt.authenticate_token(token(parents=("x" * 65537,)), secret=SECRET)
    assert not people.reads


def test_live_raw_verifier_call_census_has_only_the_admission_and_logout_exceptions():
    root = Path(__file__).resolve().parents[2]
    hits = set()
    for path in (root / "server").rglob("*.py"):
        tree = ast.parse(path.read_text())
        imports = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for item in node.names:
                    imports[item.asname or item.name] = node.module + "." + item.name
            elif isinstance(node, ast.Import):
                for item in node.names:
                    imports[item.asname or item.name.split(".")[0]] = (
                        item.name if item.asname else item.name.split(".")[0]
                    )

        def name(node, aliases=imports, is_jwt=path.name == "jwt.py"):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, "server.auth.jwt." + node.id if is_jwt else node.id)
            if isinstance(node, ast.Attribute):
                return name(node.value) + "." + node.attr
            return ""

        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and name(node.func) == "server.auth.jwt.verify_token":
                    hits.add((str(path.relative_to(root)), function.name))
    assert hits == {
        ("server/auth/jwt.py", "authenticate_token"),
        ("server/auth/oauth.py", "revoke_token"),
    }


@pytest.mark.parametrize("path", ["/v1/me/sharing-summary", "/v1/me/set-default-namespace"])
@pytest.mark.parametrize("parent", ["", "missing@example.test"])
async def test_caller_orgless_fallback_checks_the_complete_person_in_the_signup_org(
    people, token, monkeypatch, path, parent
):
    import httpx

    from server.auth import tenancy
    from server.transport.tenancy_gate import TenancyMiddleware

    monkeypatch.setattr(settings, "self_signup_org", "signups")
    people.accounts.append(("signups", "alice", "alice@example.test", True))
    people.provision("alice@example.test")
    raw = jwt.mint_token(
        "alice",
        "",
        ["*"],
        ["writer"],
        secret=SECRET,
        ttl_seconds=60,
        parent_subjects=[parent] if parent else [],
        parent_iats=[int(time.time())] if parent else [],
    )
    held = []
    admitted = []

    async def ensure_held(org, principal):
        held.append((org, principal))

    monkeypatch.setattr(tenancy, "ensure_held", ensure_held)
    app = FastAPI()
    app.add_middleware(TenancyMiddleware)

    @app.get(path)
    async def caller(request: Request):
        subject, org = await me_callers.caller_identity(request)
        admitted.append((subject, org))
        return {"subject": subject, "org": org}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            path, params={"org": "foreign"}, headers={"Authorization": f"Bearer {raw}"}
        )
    if parent:
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "AUTH_INVALID"
        assert admitted == held == []
    else:
        assert response.status_code == 200
        assert admitted == [("alice", "signups")]
        assert held == [("signups", None)]
        original = await jwt.authenticate_token(raw, secret=SECRET, fallback_org="signups")
        assert original.subject == "alice" and original.org == ""
    assert {p["org"] for p in people.reads if "org" in p} == {"signups"}
