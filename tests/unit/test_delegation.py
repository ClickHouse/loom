"""One delegation policy (server/auth/delegation.py): a child token gets
`requested ∩ parent ceiling ∩ the creator's live grants`, and the per-request
gate keeps it there. The parent/child matrix over standard, restricted,
cross-namespace and cross-org targets; narrow parents with a broad creator;
revoked parents and creators; a disabled account; expiry. Hermetic: grants,
the directory and the account lookup are faked."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import authz, context, delegation, revocation
from server.auth.delegation import DEFAULT_TTL_DAYS, delegate
from server.auth.jwt import AuthInfo, TokenError, verify_token
from server.operations.base import Role

ORG, OTHER = "o", "other"
RESTRICTED = "r-secret"
DAY = 86400


class _Directory:
    async def is_restricted(self, org: str, ns: str) -> bool:
        return ns == RESTRICTED

    async def restricted_set(self, org: str) -> frozenset[str]:
        return frozenset({RESTRICTED})

    def config_classification(self, org: str, ns: str) -> str:
        return "restricted" if ns == RESTRICTED else "standard"

    def code_entry(self, org: str, ns: str) -> None:
        return None


class _CH:
    """Answers the grants listing `resolve_my_namespaces` reads."""

    def __init__(self, grants: dict[tuple[str, str, str], Role]) -> None:
        self.grants = grants

    async def query_all(self, sql: str, params: Any = None, **k: Any) -> list[dict[str, Any]]:
        subs = set(params.get("subs", []))
        return [
            {"namespace": ns}
            for (org, ns, sub) in self.grants
            if org == params["org"] and sub in subs
        ]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """alice holds writer on a, b and the restricted namespace; nothing on c."""
    grants: dict[tuple[str, str, str], Role] = {
        (ORG, "a", "alice"): Role.WRITER,
        (ORG, "b", "alice"): Role.WRITER,
        (ORG, RESTRICTED, "alice"): Role.WRITER,
        (OTHER, "x", "alice"): Role.WRITER,
    }
    state: dict[str, Any] = {"grants": grants, "active": True}

    async def grant_role_for(org: str, ns: str, subject: str) -> Role | None:
        return grants.get((org, ns, subject))

    async def account_active(org: str, creator: str) -> bool:
        return bool(state["active"])

    async def aliases(org: str, subject: str, **k: Any) -> list[str]:
        return [subject]

    monkeypatch.setattr(authz, "_grant_role_for", grant_role_for)
    monkeypatch.setattr(authz, "directory", lambda: _Directory())
    monkeypatch.setattr(context, "directory", lambda: _Directory())
    monkeypatch.setattr(delegation, "creator_account_active", account_active)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", aliases)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH(grants))
    return state


def _login(**kw: Any) -> AuthInfo:
    """A primary flat token, as login mints it."""
    base: dict[str, Any] = {
        "subject": "alice",
        "org": ORG,
        "namespaces": ("a",),
        "roles": (Role.WRITER,),
        "is_service_account": False,
        "token_id": "login-1",
    }
    return AuthInfo(**{**base, **kw})


def _scoped() -> AuthInfo:
    return _login(roles=(), ns_roles=(("a", "writer"),), is_service_account=True, token_id="sc-1")


def _delegated_list() -> AuthInfo:
    return _login(creator="alice", parents=("login-1",), token_id="dl-1")


async def _child(parent: AuthInfo, requested: dict[str, str | None], **kw: Any) -> AuthInfo:
    """Mint the child and read it back as the gate would see it."""
    d = await delegate(
        parent,
        org=kw.pop("org", ORG),
        requested=requested,
        scoped=kw.pop("scoped", False),
        ttl_seconds=kw.pop("ttl_seconds", None),
        is_service_account=kw.pop("is_service_account", True),
    )
    return verify_token(d.mint(kw.pop("subject", "sa:bot")))


async def _reaches(token: AuthInfo, ns: str, role: Role = Role.VIEWER, org: str = ORG) -> bool:
    try:
        await authz.check_authorized(token, role, org, ns)
    except PermissionError:
        return False
    return True


async def _refused(parent: AuthInfo, requested: dict[str, str | None], **kw: Any) -> str:
    with pytest.raises(HTTPException) as err:
        await _child(parent, requested, **kw)
    assert err.value.status_code == 403
    return str(err.value.detail["message"])


# ------------------------------------------------------------------ the matrix


@pytest.mark.asyncio
async def test_a_flat_parent_wildcard_child_reaches_only_the_creators_grants(world) -> None:
    """The reported widening: `*` on a flat token used to admit every namespace
    of the org. Now it is the creator's live grants, inside the token's role."""
    child = await _child(_login(), {"*": None})
    assert child.namespaces == ("*",) and child.creator == "alice"
    assert await _reaches(child, "a") and await _reaches(child, "b", Role.WRITER)
    assert not await _reaches(child, "c")  # no grant: the bug
    assert not await _reaches(child, "a", Role.ADMIN)
    assert not await _reaches(child, RESTRICTED)  # restricted must be named
    assert not await _reaches(child, "x", org=OTHER)  # cross-org


@pytest.mark.parametrize("parent", [_scoped, _delegated_list], ids=["scoped", "delegated-list"])
@pytest.mark.asyncio
async def test_a_narrow_parent_with_a_broad_creator_cannot_reach_b(world, parent) -> None:
    assert "b" in await _refused(parent(), {"b": None})
    child = await _child(parent(), {"*": None})  # wildcard stays narrow
    assert child.namespaces == ("a",)
    assert await _reaches(child, "a") and not await _reaches(child, "b")
    assert await context_namespaces(child) == ["a"]


@pytest.mark.asyncio
async def test_named_namespaces_need_the_creators_grant_at_the_role(world) -> None:
    assert "c" in await _refused(_login(), {"c": None})
    assert "admin" in await _refused(_login(roles=(Role.ADMIN,)), {"a": "admin"})
    child = await _child(_login(), {"a": "viewer"}, scoped=True)
    assert child.ns_roles == (("a", "viewer"),)
    assert await _reaches(child, "a") and not await _reaches(child, "a", Role.WRITER)


@pytest.mark.asyncio
async def test_a_cross_org_child_is_refused(world) -> None:
    assert "org" in await _refused(_login(), {"x": None}, org=OTHER)
    # an org-wide parent names the org; the creator's grants there still decide
    child = await _child(_login(org="*"), {"x": None}, org=OTHER)
    assert child.org == OTHER and await _reaches(child, "x", org=OTHER)


@pytest.mark.asyncio
async def test_a_restricted_namespace_needs_its_two_factors_and_the_creator(world) -> None:
    child = await _child(_login(), {RESTRICTED: None}, subject="sa:r")
    assert not await _reaches(child, RESTRICTED)  # the token's own direct grant is missing
    world["grants"][(ORG, RESTRICTED, "sa:r")] = Role.VIEWER
    assert await _reaches(child, RESTRICTED)
    del world["grants"][(ORG, RESTRICTED, "alice")]
    assert not await _reaches(child, RESTRICTED)  # creator lost it


@pytest.mark.asyncio
async def test_the_creators_grants_are_rechecked_on_every_request(world) -> None:
    child = await _child(_login(), {"*": None}, subject="alice", is_service_account=False)
    assert await _reaches(child, "b")
    del world["grants"][(ORG, "b", "alice")]
    assert not await _reaches(child, "b")
    assert await context_namespaces(child) == ["a"]


@pytest.mark.asyncio
async def test_the_subjects_own_grants_do_not_widen_a_delegated_token(world) -> None:
    world["grants"][(ORG, "c", "sa:bot")] = Role.ADMIN
    child = await _child(_login(), {"a": None})
    assert not await _reaches(child, "c")


async def context_namespaces(token: AuthInfo) -> list[str]:
    context.set_current_auth(token)
    try:
        return await context.resolve_my_namespaces(ORG)
    finally:
        context.set_current_auth(None)


# ------------------------------------------------------ revocation and accounts


@pytest.mark.asyncio
async def test_revoking_the_parent_kills_the_child_and_the_grandchild(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = await _child(_login(), {"*": None})
    grandchild = await _child(child, {"*": None})
    assert grandchild.parents == ("login-1", child.token_id)
    assert grandchild.creator == "alice"
    d = await delegate(
        child,
        org=ORG,
        requested={"a": None},
        scoped=False,
        ttl_seconds=None,
        is_service_account=True,
    )
    raw_grandchild = d.mint("sa:g")
    verify_token(raw_grandchild)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({"login-1"}))
    with pytest.raises(TokenError, match="parent"):
        verify_token(raw_grandchild)


@pytest.mark.asyncio
async def test_revoking_the_creator_kills_the_child(world, monkeypatch: pytest.MonkeyPatch) -> None:
    d = await delegate(
        _login(),
        org=ORG,
        requested={"a": None},
        scoped=False,
        ttl_seconds=None,
        is_service_account=True,
    )
    raw = d.mint("sa:bot")
    monkeypatch.setattr(revocation, "_revoked_before", {"alice": int(time.time()) + 10})
    with pytest.raises(TokenError, match="creator"):
        verify_token(raw)
    # a cutoff before the child was minted spares it
    monkeypatch.setattr(revocation, "_revoked_before", {"alice": int(time.time()) - 10})
    assert verify_token(raw).creator == "alice"


@pytest.mark.asyncio
async def test_a_disabled_account_kills_the_child(world) -> None:
    child = await _child(_login(), {"*": None})
    world["active"] = False
    assert not await _reaches(child, "a")
    with pytest.raises(PermissionError):
        await authz.check_authorized(child, Role.VIEWER, ORG, None)
    assert await context_namespaces(child) == []


class _AccountCH:
    def __init__(self, row: dict[str, int] | None, fail: bool = False) -> None:
        self.row, self.fail, self.reads = row, fail, 0

    async def query_one(self, sql: str, params: Any = None) -> dict[str, int] | None:
        self.reads += 1
        if self.fail:
            raise RuntimeError("down")
        return self.row


@pytest.mark.parametrize(
    ("row", "fail", "active"),
    [
        ({"total": 0, "live": 0}, False, True),  # an operator subject owns no account rows
        ({"total": 2, "live": 1}, False, True),
        ({"total": 2, "live": 0}, False, False),  # deactivated / claim-released
        (None, True, False),  # unreadable: closed
    ],
)
@pytest.mark.asyncio
async def test_creator_account_active(monkeypatch: pytest.MonkeyPatch, row, fail, active) -> None:
    ch = _AccountCH(row, fail)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(delegation, "_account_active", {})
    assert await delegation.creator_account_active(ORG, "alice") is active
    assert await delegation.creator_account_active(ORG, "alice") is active
    assert ch.reads == (2 if fail else 1)  # a good answer is cached, a failure is not


# ------------------------------------------------------------- expiry and type


@pytest.mark.asyncio
async def test_expiry_defaults_and_never_outlives_the_parent(world) -> None:
    now = int(time.time())
    child = await _child(_login(), {"a": None})
    assert child.expires_at is not None
    assert abs(child.expires_at - (now + DEFAULT_TTL_DAYS * DAY)) < 5
    short = _login(expires_at=now + 3600)
    clipped = await _child(short, {"a": None}, ttl_seconds=30 * DAY)
    assert clipped.expires_at is not None and clipped.expires_at <= now + 3600 + 1
    assert "expired" in await _refused(_login(expires_at=now - 1), {"a": None})


@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
@pytest.mark.asyncio
async def test_org_wide_roles_are_never_delegated(world, role: str) -> None:
    boss = _login(roles=(Role.PLATFORM_ADMIN,))
    assert "never delegated" in await _refused(boss, {"a": role})
    child = await _child(boss, {"*": None})
    assert child.roles == (Role.ADMIN,)  # a platform admin's child is at most admin


@pytest.mark.asyncio
async def test_a_service_account_mints_only_service_accounts(world) -> None:
    sa = _login(subject="sa:x", is_service_account=True, creator="alice", namespaces=("*",))
    assert (await _child(sa, {"a": None}, is_service_account=False)).is_service_account
    assert (await _child(sa, {"a": None})).is_service_account


@pytest.mark.asyncio
async def test_the_chain_is_bounded(world) -> None:
    deep = _login(creator="alice", namespaces=("*",), parents=tuple(f"p{i}" for i in range(8)))
    assert "chain" in await _refused(deep, {"a": None})


@pytest.mark.asyncio
async def test_read_scope_and_write_namespaces_are_inherited(world) -> None:
    parent = _login(read_scope="own_user", read_user_id="u1", write_namespaces=("a",))
    child = await _child(parent, {"*": None})
    assert (child.read_scope, child.read_user_id, child.write_namespaces) == (
        "own_user",
        "u1",
        ("a",),
    )
    assert await _reaches(child, "a", Role.WRITER) and not await _reaches(child, "b", Role.WRITER)


def test_setup_parent_in_dev_mode_is_a_flat_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.app import me_setup
    from server.settings import settings

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    parent = me_setup._setup_parent(SimpleNamespace(headers={}), {"subject": "d", "org": "g"})
    assert (parent.subject, parent.org, parent.roles, parent.creator) == (
        "d",
        "g",
        (Role.WRITER,),
        "",
    )
