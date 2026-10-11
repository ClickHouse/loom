"""The /me Setup tab mints through the one delegation policy: the default `*` token
follows the caller's live grants, a scoped caller's `*` becomes its own namespace list,
the token never carries an org-wide role, and it expires (90 days by default)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import authz, delegation
from server.auth.jwt import AuthInfo, verify_token
from server.identity.app import me_setup as me
from server.operations.base import Role
from server.settings import settings


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str, str], Role]:
    held: dict[tuple[str, str, str], Role] = {("o", "a", "alice"): Role.WRITER}

    async def grant_role_for(org: str, ns: str, subject: str) -> Role | None:
        return held.get((org, ns, subject))

    monkeypatch.setattr(authz, "grant_role_for", grant_role_for)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    return held


async def _setup(monkeypatch: pytest.MonkeyPatch, caller: AuthInfo, body: dict) -> dict[str, Any]:
    monkeypatch.setattr("server.auth.bearer.verify_bearer", lambda token: caller)
    request: Any = SimpleNamespace(headers={"Authorization": "Bearer t"})
    return await me.me_setup_mcp_config(request, {"client": "cursor", **body})


def _caller(**kw: Any) -> AuthInfo:
    base: dict[str, Any] = {
        "subject": "alice",
        "org": "o",
        "namespaces": ("home",),
        "roles": (Role.PLATFORM_ADMIN,),
        "is_service_account": False,
        "token_id": "login-1",
    }
    return AuthInfo(**{**base, **kw})


@pytest.mark.asyncio
async def test_the_default_token_follows_live_grants_and_drops_org_wide_roles(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    out = await _setup(monkeypatch, _caller(), {})
    token = verify_token(out["token"])
    assert token.namespaces == ("*",) and token.roles == (Role.ADMIN,)
    assert token.creator == "alice" and token.parents == ("login-1",)
    assert token.expires_at is not None
    assert token.expires_at - time.time() > (delegation.DEFAULT_TTL_DAYS - 1) * 86400
    assert (out["namespace_scope"], out["read_scope"]) == ("all", "inherited_grants")


@pytest.mark.asyncio
async def test_a_scoped_caller_gets_its_own_list_not_the_org(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    grants[("o", "b", "alice")] = Role.WRITER
    scoped = _caller(namespaces=("a",), roles=(), ns_roles=(("a", "viewer"),))
    out = await _setup(monkeypatch, scoped, {})
    token = verify_token(out["token"])
    assert token.namespaces == ("a",) and token.roles == (Role.VIEWER,)
    assert out["namespace_scope"] == "single"


@pytest.mark.asyncio
async def test_a_service_account_cannot_mint_an_unrecorded_personal_token(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    sa = _caller(subject="sa:bot", is_service_account=True, creator="alice", namespaces=("*",))
    with pytest.raises(HTTPException) as err:
        await _setup(monkeypatch, sa, {"persist": False})
    assert err.value.status_code == 403
    assert "persist=true" in err.value.detail["message"]


@pytest.mark.asyncio
async def test_a_namespace_the_caller_holds_no_grant_on_is_refused(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    with pytest.raises(HTTPException) as err:
        await _setup(monkeypatch, _caller(), {"namespace": "c"})
    assert err.value.status_code == 403 and "c" in err.value.detail["message"]
    out = await _setup(monkeypatch, _caller(), {"namespace": "a"})
    token = verify_token(out["token"])
    assert token.namespaces == ("a",) and token.roles == (Role.WRITER,)
    assert (out["namespace_scope"], out["read_scope"]) == ("single", "named_namespaces")
