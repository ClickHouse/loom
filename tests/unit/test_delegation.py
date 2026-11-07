"""One delegation policy (server/auth/delegation.py): a child token gets
`requested ∩ parent ceiling ∩ the creator's live grants`, where a per-namespace
child of a primary token takes the grant alone on a standard namespace, and the
per-request gate keeps it there. The parent/child matrix over standard, restricted,
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
from server.auth.directory import ClassificationUnavailable
from server.auth.jwt import AuthInfo, TokenError, mint_token, verify_token
from server.core import grant_cache
from server.operations.base import Role, grant_rank

ORG, OTHER = "o", "other"
# Taken before tests/unit/conftest.py stubs it out for the fakes that have no store.
_REFUSE_A_REVOKED_PARENT = delegation._refuse_a_revoked_parent
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

    async def best_grant(org: str, creator: str) -> Role | None:
        held = [r for (o, _ns, sub), r in grants.items() if (o, sub) == (org, creator)]
        return max(held, default=None)

    async def aliases(org: str, subject: str, **k: Any) -> list[str]:
        return [subject]

    monkeypatch.setattr(authz, "grant_role_for", grant_role_for)
    monkeypatch.setattr(authz, "directory", lambda: _Directory())
    monkeypatch.setattr(context, "directory", lambda: _Directory())
    monkeypatch.setattr(delegation, "creator_account_active", account_active)
    monkeypatch.setattr(delegation, "creator_best_grant", best_grant)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", aliases)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH(grants))

    async def grant_map_read(org: str, subject: str) -> grant_cache.GrantMap:
        # The maps the listings ask the gate through (`authz.CachedGate`).
        ranks = {
            ns: grant_rank(r.name.lower())
            for (o, ns, s), r in grants.items()
            if o == org and s == subject
        }
        return grant_cache.GrantMap(ranks=ranks)

    monkeypatch.setattr(grant_cache, "_read", grant_map_read)
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
        "issued_at": int(time.time()),
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
async def test_a_sign_in_token_delegates_the_persons_grant_not_its_flat_role(world) -> None:
    """Every sign-in token carries `writer`; on a namespace the person
    administers, the grant decides, as it does at the gate. A delegated or
    scoped parent still caps at its own claim, and a restricted namespace
    needs the role from the token too."""
    world["grants"][(ORG, "a", "alice")] = Role.ADMIN
    child = await _child(_login(), {"a": "admin"}, scoped=True)
    assert child.ns_roles == (("a", "admin"),)
    assert await _reaches(child, "a", Role.ADMIN)
    bare = await _child(_login(), {"a": None}, scoped=True)
    assert bare.ns_roles == (("a", "admin"),)
    for parent in (_delegated_list, _scoped):
        assert "admin" in await _refused(parent(), {"a": "admin"})
        assert "admin" in await _refused(parent(), {"a": "admin"}, scoped=True)
    world["grants"][(ORG, RESTRICTED, "alice")] = Role.ADMIN
    assert "admin" in await _refused(_login(), {RESTRICTED: "admin"}, scoped=True)


@pytest.mark.asyncio
async def test_a_flat_child_keeps_the_sign_in_tokens_role(world) -> None:
    """A flat child's one role also answers an op that names no namespace,
    where the gate reads the flat role alone: it stays inside the parent's."""
    world["grants"][(ORG, "a", "alice")] = Role.ADMIN
    assert "admin" in await _refused(_login(), {"a": "admin"})
    child = await _child(_login(), {"a": None})
    assert child.roles == (Role.WRITER,)
    with pytest.raises(PermissionError):
        await authz.check_authorized(child, Role.ADMIN, ORG, None)
    assert not await _reaches(child, "a", Role.ADMIN)  # its own claim is its ceiling


@pytest.mark.asyncio
async def test_a_namespace_named_beside_star_does_not_lower_the_tokens_role(world) -> None:
    """A `*` token reads through the creator's live grants, which cap every call: a
    namespace named beside it (where it saves, a restricted repository the creator only
    reads) counts at the parent's ceiling, so the token still writes where the creator
    writes."""
    world["grants"][(ORG, RESTRICTED, "alice")] = Role.VIEWER
    child = await _child(_login(), {"a": None, "*": None, RESTRICTED: None})
    assert child.namespaces == ("a", "*", RESTRICTED) and child.roles == (Role.WRITER,)
    assert await _reaches(child, "a", Role.WRITER) and await _reaches(child, "b", Role.WRITER)
    # On the restricted namespace the creator only reads, so does the token, whatever its
    # one role, even with a direct grant of its own there.
    world["grants"][(ORG, RESTRICTED, "sa:bot")] = Role.VIEWER
    assert await _reaches(child, RESTRICTED) and not await _reaches(child, RESTRICTED, Role.WRITER)
    # A role asked for still caps the one role, and without `*` the lowest grant does.
    assert (await _child(_login(), {"*": None, "b": "viewer"})).roles == (Role.VIEWER,)
    assert (await _child(_login(), {"a": None, RESTRICTED: None})).roles == (Role.VIEWER,)


@pytest.mark.asyncio
async def test_beside_star_a_narrow_parent_still_caps_the_role(world) -> None:
    parent = _login(
        namespaces=("a", "b"),
        roles=(),
        ns_roles=(("a", "writer"), ("b", "viewer")),
        is_service_account=True,
        token_id="sc-2",
    )
    child = await _child(parent, {"*": None})
    assert child.namespaces == ("a", "b") and child.roles == (Role.VIEWER,)
    assert not await _reaches(child, "a", Role.WRITER)
    # A role asked for on `*` holds for every namespace it expands into.
    writer = _login(
        namespaces=("a", "b"),
        roles=(),
        ns_roles=(("a", "writer"), ("b", "writer")),
        is_service_account=True,
        token_id="sc-3",
    )
    assert (await _child(writer, {"*": None})).roles == (Role.WRITER,)
    asked = await _child(writer, {"*": "viewer"})
    assert asked.roles == (Role.VIEWER,) and not await _reaches(asked, "a", Role.WRITER)
    beside = await _child(writer, {"a": None, "b": None, "*": "viewer"})
    assert beside.roles == (Role.VIEWER,)


@pytest.mark.asyncio
async def test_a_viewer_flat_parent_delegates_its_subjects_grant_per_namespace(world) -> None:
    """`loom mint-token --roles viewer` for a subject who holds admin: the
    gate admits it as admin on that namespace, so a per-namespace child may be
    admin there; a flat child stays viewer."""
    world["grants"][(ORG, "a", "alice")] = Role.ADMIN
    parent = _login(roles=(Role.VIEWER,))
    child = await _child(parent, {"a": "admin"}, scoped=True)
    assert child.ns_roles == (("a", "admin"),) and await _reaches(child, "a", Role.ADMIN)
    assert (await _child(parent, {"a": None})).roles == (Role.VIEWER,)


@pytest.mark.asyncio
async def test_an_unreadable_classification_keeps_the_token_role_cap(world, monkeypatch) -> None:
    """The restricted predicate fails closed: with the directory unreadable the
    mint stays inside the parent token's role, as the gate would."""
    world["grants"][(ORG, "a", "alice")] = Role.ADMIN

    class _Down(_Directory):
        async def is_restricted(self, org: str, ns: str) -> bool:
            raise ClassificationUnavailable("down")

    monkeypatch.setattr(authz, "directory", lambda: _Down())
    assert "admin" in await _refused(_login(), {"a": "admin"}, scoped=True)


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
async def test_a_restricted_namespace_outside_ns_write_is_read_only(world) -> None:
    child = _login(
        namespaces=("a", RESTRICTED),
        write_namespaces=("a",),
        creator="alice",
        parents=("login-1",),
        subject="sa:r",
    )
    world["grants"][(ORG, RESTRICTED, "sa:r")] = Role.WRITER
    assert await _reaches(child, RESTRICTED)
    assert not await _reaches(child, RESTRICTED, Role.WRITER)
    assert await _reaches(child, "a", Role.WRITER)


@pytest.mark.asyncio
async def test_the_creators_grants_are_rechecked_on_every_request(world) -> None:
    child = await _child(_login(), {"*": None}, subject="alice", is_service_account=False)
    assert await _reaches(child, "b")
    del world["grants"][(ORG, "b", "alice")]
    grant_cache.invalidate(ORG, ["alice"])
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
async def test_ending_a_third_party_clients_session_kills_what_it_minted(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = int(time.time()) + 3600

    async def minted(parent: AuthInfo) -> str:
        d = await delegate(
            parent,
            org=ORG,
            requested={"a": None},
            scoped=False,
            ttl_seconds=None,
            is_service_account=True,
        )
        return d.mint("sa:bot")

    third_party = await minted(_login(session_id="fam-mcp", expires_at=exp))
    own = await minted(_login(session_id="fam-cli", first_party=True, expires_at=exp))
    assert verify_token(third_party).parents == ("login-1", "fam-mcp")
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({"fam-mcp", "fam-cli"}))
    with pytest.raises(TokenError, match="parent"):
        verify_token(third_party)
    assert verify_token(own).parents == ("login-1",)  # outlives the session it came from


@pytest.mark.asyncio
async def test_a_third_party_session_counts_toward_the_chain_limit(world) -> None:
    exp = int(time.time()) + 3600
    seven = tuple(f"t{i}" for i in range(7))  # with the parent's own id: 8 ancestors
    own = _login(parents=seven, session_id="fam-cli", first_party=True, expires_at=exp)
    assert len((await _child(own, {"a": None})).parents) == 8
    third_party = _login(parents=seven, session_id="fam-mcp", expires_at=exp)
    assert "limited to 8 ancestors" in await _refused(third_party, {"a": None})


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


async def _no_aliases(org: str, subject: str, **k: Any) -> list[str]:
    return [subject]


@pytest.mark.parametrize(
    ("row", "fail", "active", "best"),
    [
        ({}, False, True, None),  # an operator subject owns and holds nothing
        ({"owned": 2, "owned_live": 1, "granted": 1, "best_rank": 2}, False, True, Role.WRITER),
        ({"owned": 2, "owned_live": 0, "granted": 1, "best_rank": 2}, False, False, Role.WRITER),
        ({"granted": 3, "best_rank": 0}, False, False, None),  # every grant revoked
        (None, True, False, None),  # unreadable: closed
    ],
)
@pytest.mark.asyncio
async def test_creator_account_active(
    monkeypatch: pytest.MonkeyPatch, row, fail, active, best
) -> None:
    ch = _AccountCH(row, fail)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", _no_aliases)
    monkeypatch.setattr(delegation, "_account_active", {})
    assert await delegation.creator_account_active(ORG, "alice") is active
    assert await delegation.creator_account_active(ORG, "alice") is active
    assert await delegation.creator_best_grant(ORG, "alice") == best
    assert ch.reads == (3 if fail else 1)  # a good answer is cached, a failure is not


class _TablesCH:
    """loom.namespaces and loom.namespace_grants as the latest row per key:
    what release_claim reads and writes, and what the account query counts."""

    def __init__(self) -> None:
        self.namespaces: dict[tuple[str, str], dict[str, Any]] = {}
        self.grants: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
        for r in rows:
            if table == "loom.namespaces":
                self.namespaces[(r["org"], r["namespace"])] = dict(r)
            else:
                self.grants[(r["org"], r["namespace"], r["subject"])] = dict(r)

    async def query_all(self, sql: str, params: Any = None, **k: Any) -> list[dict[str, Any]]:
        return [  # release_claim's live grants on one namespace
            g
            for (org, ns, _), g in self.grants.items()
            if (org, ns) == (params["org"], params["ns"]) and g["revoked_at"] is None
        ]

    async def query_one(self, sql: str, params: Any = None, **k: Any) -> dict[str, Any] | None:
        if "SELECT *" in sql:
            return self.namespaces.get((params["org"], params["ns"]))
        owners = params["subs"] if "lower(owner_email) IN {subs:" in sql else [params.get("sub")]
        owned = [
            n
            for (org, _), n in self.namespaces.items()
            if org == params["org"] and n["owner_email"].lower() in owners
        ]
        held = [
            g
            for (org, _, sub), g in self.grants.items()
            if org == params["org"]
            and sub.lower() in params["subs"]
            and g.get("status", "active") == "active"
        ]
        rank = {"admin": 3, "writer": 2, "viewer": 1}
        live = [rank.get(g["role"], 0) for g in held if g["revoked_at"] is None]
        return {
            "owned": len(owned),
            "owned_live": sum(1 for n in owned if n["active"]),
            "granted": len(held),
            "best_rank": max(live, default=0),
        }


@pytest.mark.asyncio
async def test_a_claim_release_disables_the_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    """release_claim blanks owner_email and tombstones every grant; the
    account check must still read the released creator as disabled."""
    from server.identity.app.claim_release import release_claim

    ch = _TablesCH()
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": ORG,
                "namespace": "a",
                "owner_email": "alice",
                "contact_email": "a@x",
                "password_hash": "h",
                "active": True,
                "description": "",
                "updated_at": None,
            }
        ],
    )
    await ch.insert(
        "loom.namespace_grants",
        [
            {
                "org": ORG,
                "namespace": "a",
                "subject": "alice",
                "role": "admin",
                "granted_by": "alice",
                "revoked_at": None,
                "status": "active",
            }
        ],
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", _no_aliases)
    monkeypatch.setattr(delegation, "_account_active", {})
    assert await delegation.creator_account_active(ORG, "alice") is True
    await release_claim(org=ORG, namespace="a", reason="", actor_subject="op", ch=ch)
    released = ch.namespaces[(ORG, "a")]
    assert released["owner_email"] == ""
    # The handle-to-email binding the claim cap reads is kept, for that cap alone.
    assert released["released_aliases"] == ["a@x", "alice"]
    monkeypatch.setattr(delegation, "_account_active", {})
    assert await delegation.creator_account_active(ORG, "alice") is False


@pytest.mark.asyncio
async def test_an_owner_stored_under_the_email_alias_is_the_same_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _TablesCH()
    ch.namespaces[(ORG, "a")] = {"org": ORG, "owner_email": "alice@x", "active": False}

    async def aliases(org: str, subject: str, **k: Any) -> list[str]:
        return ["alice", "alice@x"]

    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", aliases)
    monkeypatch.setattr(delegation, "_account_active", {})
    assert await delegation.creator_account_active(ORG, "alice") is False  # deactivated


@pytest.mark.asyncio
async def test_the_account_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _AccountCH({})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", _no_aliases)
    monkeypatch.setattr(delegation, "_account_active", {})
    monkeypatch.setattr(delegation, "_ACCOUNT_CACHE_MAX", 3)
    for i in range(10):
        await delegation.creator_account_active(ORG, f"c{i}")
    assert len(delegation._account_active) <= 3


@pytest.mark.asyncio
async def test_another_orgs_request_is_refused_before_any_account_read(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []

    async def account_active(org: str, creator: str) -> bool:
        reads.append(org)
        return True

    child = await _child(_login(), {"*": None})
    monkeypatch.setattr(delegation, "creator_account_active", account_active)
    for org in ("evil-1", "evil-2"):
        with pytest.raises(PermissionError, match="cannot act in org"):
            await authz.check_authorized(child, Role.VIEWER, org, "a")
    assert reads == []


@pytest.mark.asyncio
async def test_an_org_level_write_rechecks_the_creators_grants(world) -> None:
    child = await _child(_login(), {"*": None})
    await authz.check_authorized(child, Role.WRITER, ORG, None)
    await authz.check_authorized(child, Role.VIEWER, ORG, None)
    for key in [k for k in world["grants"] if k[0] == ORG]:
        del world["grants"][key]
    with pytest.raises(PermissionError, match="no grant"):
        await authz.check_authorized(child, Role.WRITER, ORG, None)
    await authz.check_authorized(child, Role.VIEWER, ORG, None)  # reads stay namespace-gated


@pytest.mark.asyncio
async def test_revoking_a_service_account_by_subject_kills_its_children(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard revokes a service account by its `sa_subject`."""
    sa = await _child(_login(), {"a": None}, subject="sa:parent", scoped=True)
    child = await _child(sa, {"a": None}, subject="sa:child", scoped=True)
    assert child.parent_subjects == ("alice", "sa:parent")
    d = await delegate(
        child,
        org=ORG,
        requested={"a": None},
        scoped=True,
        ttl_seconds=None,
        is_service_account=True,
    )
    raw_grandchild = d.mint("sa:grandchild")
    raw_child = (
        await delegate(
            sa,
            org=ORG,
            requested={"a": None},
            scoped=True,
            ttl_seconds=None,
            is_service_account=True,
        )
    ).mint("sa:child2")
    verify_token(raw_grandchild)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({"sa:parent"}))
    for raw in (raw_child, raw_grandchild):
        with pytest.raises(TokenError, match="parent"):
            verify_token(raw)


def _old_sa(now: int) -> AuthInfo:
    """sa:old, delegated from alice two hours ago and issued an hour ago."""
    return _login(
        subject="sa:old",
        creator="alice",
        parents=("login-1",),
        parent_subjects=("alice",),
        parent_iats=(now - 7200,),
        token_id="old-1",
        issued_at=now - 3600,
    )


async def _mint_from(parent: AuthInfo, subject: str) -> str:
    d = await delegate(
        parent,
        org=ORG,
        requested={"a": None},
        scoped=False,
        ttl_seconds=None,
        is_service_account=True,
    )
    return d.mint(subject)


@pytest.mark.asyncio
async def test_an_ancestor_cutoff_is_judged_by_the_ancestors_own_iat(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent issued before a cutoff dies, and so do its child and grandchild
    even though both were issued after it."""
    now = int(time.time())
    child = verify_token(await _mint_from(_old_sa(now), "sa:child"))
    assert child.parent_iats == (now - 7200, now - 3600)
    raw_child = await _mint_from(_old_sa(now), "sa:c")
    raw_grandchild = await _mint_from(child, "sa:g")
    monkeypatch.setattr(revocation, "_revoked_before", {"sa:old": now - 60})
    for raw in (raw_child, raw_grandchild):
        with pytest.raises(TokenError, match="parent"):
            verify_token(raw)
    # the creator's cutoff is judged by the root's iat
    monkeypatch.setattr(revocation, "_revoked_before", {"alice": now - 5000})
    with pytest.raises(TokenError, match="creator"):
        verify_token(raw_grandchild)


@pytest.mark.asyncio
async def test_an_ancestor_issued_after_the_cutoff_survives_it(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    raw = await _mint_from(_old_sa(now), "sa:c")
    monkeypatch.setattr(revocation, "_revoked_before", {"sa:old": now - 4000, "alice": now - 8000})
    assert verify_token(raw).parent_subjects == ("alice", "sa:old")


@pytest.mark.parametrize("iats", [None, [1, 2], ["1"], [True]])
def test_parent_subjects_without_aligned_parent_iats_are_refused(world, iats: Any) -> None:
    raw = mint_token(
        subject="sa:x",
        org=ORG,
        namespaces=["a"],
        roles=["writer"],
        parents=["p-1"],
        parent_subjects=["alice"],
        parent_iats=iats,
        creator="alice",
    )
    with pytest.raises(TokenError, match="parent_iats"):
        verify_token(raw)


@pytest.mark.asyncio
async def test_a_parent_without_an_issue_time_cannot_delegate(world) -> None:
    assert "issue time" in await _refused(_login(issued_at=None), {"a": None})


@pytest.mark.asyncio
async def test_a_known_creator_grant_is_used_and_the_lookup_is_memoised(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = await _child(_login(), {"*": None})
    reads: list[str] = []

    async def counting(org: str, ns: str, subject: str) -> Role | None:
        reads.append(ns)
        return world["grants"].get((org, ns, subject))

    monkeypatch.setattr(authz, "grant_role_for", counting)
    await authz.check_authorized(child, Role.VIEWER, ORG, "c", known_grant=Role.VIEWER)
    assert reads == []
    context.set_current_auth(child)
    try:
        for _ in range(3):
            await authz.check_authorized(child, Role.VIEWER, ORG, "a")
    finally:
        context.set_current_auth(None)
    assert reads == ["a"]


@pytest.mark.asyncio
async def test_ttl_days_rounds_up(world) -> None:
    d = await delegate(
        _login(),
        org=ORG,
        requested={"a": None},
        scoped=False,
        ttl_seconds=3600,
        is_service_account=True,
    )
    assert d.ttl_days == 1


def test_the_revocation_stale_limit_must_exceed_the_refresh_interval() -> None:
    from pydantic import ValidationError

    from server.settings.catalog import AuthSettings

    with pytest.raises(ValidationError, match="MAX_STALE"):
        AuthSettings(jwt_revocation_cache_ttl_seconds=60, jwt_revocation_max_stale_seconds=60)
    AuthSettings(jwt_revocation_cache_ttl_seconds=30, jwt_revocation_max_stale_seconds=600)


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
    assert "service-account" in await _refused(sa, {"a": None}, is_service_account=False)
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


@pytest.mark.asyncio
async def test_a_scoped_child_cannot_write_outside_the_parents_write_claim(world) -> None:
    """Reads a and b, writes only a: a scoped writer child for b is refused at
    mint, one naming no role on b is minted viewer there, and one that carries
    writer on b anyway is refused writing b."""
    parent = _login(
        creator="alice", parents=("login-1",), namespaces=("a", "b"), write_namespaces=("a",)
    )
    assert not await _reaches(parent, "b", Role.WRITER)
    assert "b" in await _refused(parent, {"b": "writer"}, scoped=True)
    child = await _child(parent, {"a": None, "b": None}, scoped=True)
    assert child.ns_roles == (("a", "writer"), ("b", "viewer")), child.ns_roles
    assert child.write_namespaces == ("a",)
    assert await _reaches(child, "b") and not await _reaches(child, "b", Role.WRITER)
    minted = _login(
        roles=(),
        ns_roles=(("a", "writer"), ("b", "writer")),
        namespaces=("a", "b"),
        write_namespaces=("a",),
        creator="alice",
        parents=("dl-1",),
        subject="sa:bot",
    )
    assert await _reaches(minted, "a", Role.WRITER)
    assert not await _reaches(minted, "b", Role.WRITER)


@pytest.mark.asyncio
async def test_a_primary_scoped_token_writes_by_its_role_map(world) -> None:
    token = _login(roles=(), ns_roles=(("a", "writer"), ("b", "viewer")), namespaces=("a", "b"))
    assert await _reaches(token, "a", Role.WRITER)
    assert await _reaches(token, "b") and not await _reaches(token, "b", Role.WRITER)
    bound = _login(**{**token.__dict__, "write_namespaces": ("b",)})
    assert not await _reaches(bound, "a", Role.WRITER)


def test_the_dev_mode_parent_is_a_flat_writer() -> None:
    from server.identity.app import me_service_accounts

    parent = me_service_accounts.dev_parent(
        SimpleNamespace(headers={}), {"subject": "d", "org": "g"}
    )
    assert (parent.subject, parent.org, parent.roles, parent.creator) == (
        "d",
        "g",
        (Role.WRITER,),
        "",
    )


@pytest.mark.parametrize("raw", [True, 1.9, "7", float("inf"), float("nan"), [7], -1, 3651])
def test_ttl_days_must_be_an_integer_in_range(raw: Any) -> None:
    from server.identity.app.me_service_accounts import parse_ttl_days

    with pytest.raises(HTTPException) as err:
        parse_ttl_days(raw)
    assert err.value.status_code == 422


def test_ttl_days_accepts_an_integral_number() -> None:
    from server.identity.app.me_service_accounts import parse_ttl_days

    assert parse_ttl_days(7) == parse_ttl_days(7.0) == 7 * 86400
    assert parse_ttl_days(None) is None
    assert parse_ttl_days(0) == 0  # never expires


@pytest.mark.parametrize(
    ("revoked", "refused"),
    [
        ({"alice": None}, True),  # revoked outright: SCIM's deactivation, or revoke-token
        ({"alice": 2**40}, True),  # a cutoff past the parent's issue time
        ({"alice": 1}, False),  # a cutoff before it: the parent was made after
        ({"login-1": None}, True),  # an ancestor token revoked
        ({}, False),
    ],
)
async def test_a_mint_reads_the_parents_revocations_from_the_store(
    world, monkeypatch, revoked: dict[str, int | None], refused: bool
) -> None:
    # A replica whose snapshot is behind takes the parent at the gate; the mint must not.
    monkeypatch.setattr(delegation, "_refuse_a_revoked_parent", _REFUSE_A_REVOKED_PARENT)
    asked: list[list[str]] = []

    async def cutoffs(subjects: list[str]) -> dict[str, int | None]:
        asked.append(subjects)
        return {s: c for s, c in revoked.items() if s in subjects}

    monkeypatch.setattr(revocation, "cutoffs", cutoffs)
    if refused:
        with pytest.raises(HTTPException) as e:
            await _child(_delegated_list(), {"a": None})
        assert e.value.status_code == 401
    else:
        await _child(_delegated_list(), {"a": None})
    assert asked == [["alice", "dl-1", "login-1"]]  # the subject, this token, its ancestor


async def test_a_mint_judges_each_ancestor_by_its_own_issue_time(world, monkeypatch) -> None:
    monkeypatch.setattr(delegation, "_refuse_a_revoked_parent", _REFUSE_A_REVOKED_PARENT)
    now = int(time.time())
    parent = _login(
        subject="sa:mid",
        creator="alice",
        token_id="t2",
        parents=("t1",),
        parent_subjects=("sa:root",),
        parent_iats=(now - 100,),
        issued_at=now,
    )

    async def cutoffs(subjects: list[str]) -> dict[str, int | None]:
        return {"sa:root": now - 50}  # narrowed after the root ancestor was issued

    monkeypatch.setattr(revocation, "cutoffs", cutoffs)
    with pytest.raises(HTTPException) as e:
        await _child(parent, {"a": None})
    assert e.value.status_code == 401


async def test_a_mint_from_an_ended_sign_in_session_is_refused(world, monkeypatch) -> None:
    # A logout ends a first-party session too; a stale replica still takes its token.
    monkeypatch.setattr(delegation, "_refuse_a_revoked_parent", _REFUSE_A_REVOKED_PARENT)

    async def cutoffs(subjects: list[str]) -> dict[str, int | None]:
        return {}

    async def revoked_outright(jti: str) -> bool:
        return jti == "sess-1"

    monkeypatch.setattr(revocation, "cutoffs", cutoffs)
    monkeypatch.setattr(revocation, "revoked_outright", revoked_outright)
    parent = _login(session_id="sess-1", first_party=True)
    with pytest.raises(HTTPException) as e:
        await _child(parent, {"a": None})
    assert e.value.status_code == 401


async def test_a_mint_whose_revocation_read_fails_mints_nothing(world, monkeypatch) -> None:
    monkeypatch.setattr(delegation, "_refuse_a_revoked_parent", _REFUSE_A_REVOKED_PARENT)

    async def cutoffs(subjects: list[str]) -> dict[str, int | None]:
        raise RuntimeError("ClickHouse is away")

    monkeypatch.setattr(revocation, "cutoffs", cutoffs)
    with pytest.raises(RuntimeError):  # the caller's error: no child, no saved row
        await _child(_delegated_list(), {"a": None})
