"""Who reaches `/v1/admin/*`. Deployment-wide endpoints need PLATFORM_ADMIN;
the org-scoped ones (token.mint, namespace.adopt, claim_release) need ORG_ADMIN
pinned to the caller's own org. A per-namespace admin, flat or scoped, never
passes. Every route of the two admin routers and of the model-setup router
(`/v1/admin/models`) is iterated, so a new one there cannot be missed;
`/v1/admin/claim_release` checks inside its handler and has its own tests
below."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
from server.insights.app import admin_routes as admin
from server.insights.app import provider_routes
from server.settings import settings

ORG_SCOPED = {"/v1/admin/token.mint", "/v1/admin/namespace.adopt"}

CALLERS: dict[str, dict[str, Any]] = {
    "namespace-admin": {"roles": ["admin"]},
    "scoped-admin": {"roles": [], "ns_roles": {"n": "admin"}},
    "org-admin": {"roles": ["org_admin"]},
    "platform-admin": {"roles": ["platform_admin"]},
}


def _token(caller: str, org: str = "acme") -> str:
    return mint_token(
        subject=f"{caller}@x", org=org, namespaces=["n"], ttl_seconds=60, **CALLERS[caller]
    )


def _routes() -> list[tuple[str, APIRoute]]:
    return [
        (r.path, r)
        for router in (admin.admin_router, admin.org_admin_router, provider_routes.provider_router)
        for r in router.routes
        if isinstance(r, APIRoute)
    ]


def test_the_org_scoped_set_is_exactly_the_org_admin_router() -> None:
    assert {r.path for r in admin.org_admin_router.routes} == ORG_SCOPED
    assert not {r.path for r in admin.admin_router.routes} & ORG_SCOPED


@pytest.mark.parametrize(("path", "route"), _routes(), ids=[p for p, _ in _routes()])
@pytest.mark.parametrize("caller", list(CALLERS))
@pytest.mark.asyncio
async def test_every_admin_route_admits_only_its_authority(
    monkeypatch: pytest.MonkeyPatch, path: str, route: APIRoute, caller: str
) -> None:
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    gates = [d.dependency for d in route.dependencies]
    if path.startswith("/v1/admin/models"):
        expected = provider_routes.require_model_operator
    elif path in ORG_SCOPED:
        expected = admin.require_org_admin
    else:
        expected = admin.require_admin
    assert gates == [expected]
    request: Any = SimpleNamespace(headers={"Authorization": f"Bearer {_token(caller)}"})
    allowed = caller == "platform-admin" or (caller == "org-admin" and path in ORG_SCOPED)
    if allowed:
        admitted = await gates[0](request)
        # require_model_operator answers None once it admits
        assert admitted is not None or expected is provider_routes.require_model_operator
        return
    with pytest.raises(HTTPException) as err:
        await gates[0](request)
    assert err.value.status_code == 403


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/admin/token.mint", {"subject": "s", "org": "globex", "namespaces": ["n"]}),
        ("/v1/admin/namespace.adopt", {"org": "globex", "namespace": "n", "new_admin": "eve"}),
        ("/v1/admin/claim_release", {"org": "globex", "namespace": "n"}),
    ],
)
def test_an_org_admin_of_another_org_is_refused(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    from server.identity.app import claim_release

    def no_client() -> object:
        raise AssertionError("refused before any read or write")

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr("server.core.ch.get_client", no_client)
    app = FastAPI()
    app.include_router(admin.org_admin_router)
    app.include_router(claim_release.router)
    r = TestClient(app).post(
        path, json=body, headers={"Authorization": f"Bearer {_token('org-admin')}"}
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"


@pytest.mark.parametrize("caller", ["namespace-admin", "scoped-admin"])
def test_claim_release_admits_no_namespace_admin(
    monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    from server.identity.app import claim_release

    def no_client() -> object:
        raise AssertionError("refused before any read or write")

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr("server.core.ch.get_client", no_client)
    app = FastAPI()
    app.include_router(claim_release.router)
    r = TestClient(app).post(
        "/v1/admin/claim_release",
        json={"org": "acme", "namespace": "n"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert r.status_code == 403, r.text


def test_auth_off_leaves_the_gates_open(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    request: Any = SimpleNamespace(headers={})
    assert asyncio.run(admin.require_admin(request)) is None
    assert asyncio.run(admin.require_org_admin(request)) is None
