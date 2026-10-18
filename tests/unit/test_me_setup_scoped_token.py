"""The dashboard's Settings page mints through the one delegation policy: the default `*` token
follows the caller's live grants, a scoped caller's `*` becomes its own namespace list,
the token never carries an org-wide role, it expires (90 days by default), and it is
saved to the caller's token list."""

from __future__ import annotations

import dataclasses
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import authz, delegation
from server.auth.jwt import AuthInfo, verify_token
from server.core import grant_cache
from server.identity.app import me_setup as me
from server.operations.base import Role, grant_rank
from server.settings import settings

saved: list[dict[str, Any]] = []


class _CH:
    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        assert table == "loom.service_accounts"
        saved.extend(rows)

    async def query_all(self, sql: str, **kw: Any) -> list[dict[str, Any]]:
        return []  # no restricted namespace, no ranked grants, no kinds

    async def query_one(self, sql: str, **kw: Any) -> dict[str, Any] | None:
        return None  # no saved pick


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str, str], Role]:
    held: dict[tuple[str, str, str], Role] = {("o", "a", "alice"): Role.WRITER}

    async def grant_role_for(org: str, ns: str, subject: str) -> Role | None:
        return held.get((org, ns, subject))

    async def grant_rows(org: str, subject: str) -> grant_cache.GrantMap:
        mine = {ns: role for (o, ns, sub), role in held.items() if (o, sub) == (org, subject)}
        return grant_cache.GrantMap(
            ranks={ns: grant_rank(r.name.lower()) for ns, r in mine.items()}
        )

    async def account_active(org: str, creator: str) -> bool:
        return True

    monkeypatch.setattr(authz, "grant_role_for", grant_role_for)
    # What the minted token's default reads: its creator's account and live grants.
    monkeypatch.setattr(grant_cache, "_read", grant_rows)
    monkeypatch.setattr("server.auth.delegation.creator_account_active", account_active)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    saved.clear()
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
        "issued_at": int(time.time()),
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
    assert out["namespaces"] == ["*"]


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
    assert (out["read_scope"], out["namespaces"]) == ("named_namespaces", ["a"])


@pytest.mark.asyncio
async def test_every_token_is_saved_under_its_own_subject(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    out = await _setup(monkeypatch, _caller(), {"persist": False})  # retired: still saved
    token = verify_token(out["token"])
    [row] = saved
    assert token.subject == row["sa_subject"] == out["sa_subject"]
    assert token.is_service_account and row["created_by"] == "alice"
    assert out["ignored_args"] == ["persist"]


@pytest.mark.asyncio
async def test_a_service_account_mints_a_saved_service_account_token(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    sa = _caller(subject="sa:bot", is_service_account=True, creator="alice", namespaces=("*",))
    out = await _setup(monkeypatch, sa, {})
    assert verify_token(out["token"]).is_service_account and "ignored_args" not in out
    assert [row["created_by"] for row in saved] == ["sa:bot"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl_days", [-1, 3651, 10**9])
async def test_a_ttl_outside_ten_years_is_a_422(
    monkeypatch: pytest.MonkeyPatch, grants: dict, ttl_days: int
) -> None:
    with pytest.raises(HTTPException) as err:
        await _setup(monkeypatch, _caller(), {"ttl_days": ttl_days})
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_the_saved_row_is_an_mcp_row_in_the_token_list(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    out = await _setup(monkeypatch, _caller(), {"ttl_days": 30})
    [row] = saved
    sa_hash = out["sa_subject"].removeprefix("sa:cursor-")
    assert len(sa_hash) == 6 and row["name"].startswith("Cursor · ")
    assert row["name"].endswith(f" · {sa_hash}")
    assert row["expires_at"] == row["created_at"] + timedelta(days=30)
    assert row["version"] > 0
    volatile = {"name", "created_at", "expires_at", "version"}
    assert {k: v for k, v in row.items() if k not in volatile} == {
        "org": "o",
        "sa_subject": out["sa_subject"],
        "namespaces": ["*"],
        "roles": ["admin"],
        "ns_roles": "{}",  # the column default, which the row left it to before
        "ttl_days": 30,
        "created_by": "alice",
        "revoked_at": None,
        "revoked_by": "",
        "kind": "mcp",
        "client": "cursor",
        "inherit_creator_grants": True,
    }


@pytest.mark.asyncio
async def test_the_label_needs_no_revocation_list_once_the_token_is_saved(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    # The row is saved before the label is worked out; a revocation list not
    # loaded (or stale) then must not fail the call and strand an unseen token.
    from server.auth import revocation

    monkeypatch.setattr(revocation, "loaded", lambda: False)
    out = await _setup(monkeypatch, _caller(), {"namespace": "a"})
    assert out["namespace"] == "a" and len(saved) == 1


# The delegations the Setup page mints (scoped=False: the default `*`, and a
# named namespace plus a restricted one) and a scoped caller's.
_SHAPES = [
    ({"*": None}, False),
    ({"a": None, "secret": None}, False),
    ({"a": None}, True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "scoped"), _SHAPES)
async def test_the_label_identity_is_the_one_the_token_verifies_as(
    monkeypatch: pytest.MonkeyPatch, grants: dict, requested: dict, scoped: bool
) -> None:
    grants[("o", "secret", "alice")] = Role.WRITER
    caller = (
        _caller(namespaces=("a",), roles=(), ns_roles=(("a", "writer"),)) if scoped else _caller()
    )
    delegated = await delegation.delegate(
        caller,
        org="o",
        requested=requested,
        scoped=scoped,
        ttl_seconds=3600,
        is_service_account=True,
    )
    verified = verify_token(delegated.mint("sa:cursor-ab12cd"))
    built = delegated.as_auth("sa:cursor-ab12cd")
    # Set at verification only: the token's id and its iat/exp.
    minted_only = {"token_id", "issued_at", "expires_at"}
    for field in dataclasses.fields(AuthInfo):
        if field.name not in minted_only:
            assert getattr(built, field.name) == getattr(verified, field.name), field.name


@pytest.mark.asyncio
async def test_the_page_shows_the_expiry_the_saved_row_holds(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    # A clock that moves between reads: the response must not read it again.
    ticks = iter(range(100))
    start = me.naive_utc_now_seconds()
    monkeypatch.setattr(me, "naive_utc_now_seconds", lambda: start + timedelta(seconds=next(ticks)))
    out = await _setup(monkeypatch, _caller(), {"ttl_days": 30})
    [row] = saved
    assert out["expires_at"] == row["expires_at"].isoformat()
