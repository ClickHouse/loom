"""Knowledge sharing (docs/architecture/sharing.md): a gateway's end users, the
principals of a request, collections inherited down a tree, and the rows the
three new tables hold."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.auth import gateway, sharing
from server.auth.jwt import AuthInfo, TokenError, mint_token, verify_token
from server.core import grant_cache
from server.core.grant_cache import GrantMap, watermark_row
from server.core.principals import personal_name
from server.identity.app.collections import check_placement
from server.identity.app.scim_groups import COLUMNS as GROUP_COLUMNS
from server.identity.app.scim_groups import group_row
from server.identity.store.collections import COLUMNS as COLLECTION_COLUMNS
from server.identity.store.collections import collection_row
from server.operations.base import Role
from tests import end_user_issuer
from tests.unit.schema_ddl import column_names

ORG = "acme"
SECRET = "s" * 32


# One key for the module's end-user issuer (tests/end_user_issuer.py).
_PRIVATE, _PUBLIC = end_user_issuer.new_key()


@pytest.fixture(autouse=True)
def _trusts_the_test_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(
        settings, "jwt_end_user_issuers_json", end_user_issuer.issuers_setting(_PUBLIC)
    )


def _token(user: str = "u-1", org: str = ORG, **kw: Any) -> str:
    return end_user_issuer.mint(org, user, private_pem=_PRIVATE, **kw)


# ── end-user tokens ───────────────────────────────────────────────────────


def test_an_end_user_token_runs_as_its_user_and_nothing_more() -> None:
    auth = verify_token(_token("u-1", agent="helper"))
    assert auth.subject == "user:u-1"
    assert auth.org == ORG
    assert auth.namespaces == () and auth.ns_roles == () and auth.write_namespaces == ()
    assert auth.roles == (Role.VIEWER,) and not auth.is_service_account
    assert (auth.read_scope, auth.read_user_id) == ("own_user", "u-1")
    assert auth.impersonated_by == end_user_issuer.ISSUER
    assert auth.agent_principal == "agent:helper"


@pytest.mark.parametrize(
    ("user", "claims"),
    [
        ("Alice", {}),  # ids are lowercase: two spellings never name one user
        ("a b", {}),
        ("u-1", {"org": "*"}),
        ("u-1", {"org": ""}),
        ("u-1", {"agent": "group:finance"}),
        ("u-1", {"agent": "agent:Helper"}),
        ("u-1", {"namespaces": ["team"]}),
        ("u-1", {"roles": ["admin"]}),
        ("u-1", {"read_scope": "namespace"}),
        ("u-1", {"iss": "loom"}),  # its key signs its own issuer's tokens alone
    ],
)
def test_an_end_user_token_names_one_user_of_one_org_and_nothing_else(
    user: str, claims: dict[str, Any]
) -> None:
    with pytest.raises(TokenError):
        verify_token(_token(user, **claims))


def test_an_end_user_token_is_short_lived() -> None:
    from server.settings import settings

    with pytest.raises(TokenError, match="lives at most"):
        verify_token(_token(ttl_seconds=settings.jwt_end_user_max_ttl_seconds + 120))
    with pytest.raises(TokenError, match="expired"):
        verify_token(_token(ttl_seconds=-120))


def test_an_end_user_key_signs_nothing_but_end_users() -> None:
    """A Loom-shaped admin token under the end-user key is refused: the key
    never verifies a token as anything but its issuer's end user."""
    forged = _token(sub="sa:admin")
    with pytest.raises(TokenError, match="names one user"):
        verify_token(forged)


@pytest.mark.parametrize("subject", ["user:alice", "group:finance", "agent:helper", "everyone"])
def test_no_loom_token_is_minted_for_a_grant_principal(subject: str) -> None:
    with pytest.raises(ValueError, match="grant principal"):
        mint_token(subject, ORG, ["team"], ["viewer"], secret=SECRET)


# ── the tree ──────────────────────────────────────────────────────────────


def _tree(**parents: str) -> sharing.Tree:
    return sharing.Tree(parent=dict(parents))


def test_a_lineage_is_nearest_first_and_stops_at_a_cycle() -> None:
    tree = _tree(a="", b="a", c="b")
    assert tree.lineage("c") == ["c", "b", "a"]
    looped = _tree(a="b", b="a")
    assert looped.lineage("a") == ["a", "b"]


def test_a_lineage_reads_at_most_max_depth_ancestors() -> None:
    names = [f"c{i}" for i in range(12)]
    tree = _tree(**{n: (names[i - 1] if i else "") for i, n in enumerate(names)})
    assert len(tree.lineage("c11")) == sharing.MAX_DEPTH + 1


def test_descendants_follow_children_down() -> None:
    tree = _tree(a="", b="a", c="b", d="")
    assert tree.with_descendants({"a"}) == {"a", "b", "c"}
    assert tree.height("a") == 2 and tree.height("c") == 0


def test_a_collection_cannot_move_under_itself() -> None:
    tree = _tree(a="", b="a", c="b")
    with pytest.raises(Exception, match="cannot hold itself"):
        check_placement(tree, "a", "c")


def test_a_move_that_would_nest_too_deep_is_refused() -> None:
    names = [f"c{i}" for i in range(sharing.MAX_DEPTH)]
    tree = _tree(**{n: (names[i - 1] if i else "") for i, n in enumerate(names)}, x="", y="x")
    check_placement(tree, "x", names[-2])  # x + y below it: 7 + 1 levels under the root
    with pytest.raises(Exception, match="nest at most"):
        check_placement(tree, "x", names[-1])


def test_only_a_collection_that_is_not_personal_holds_one() -> None:
    tree = sharing.Tree(parent={"col-a": ""})
    with pytest.raises(Exception, match="not a collection"):
        check_placement(tree, "col-a", "col-elsewhere")
    with pytest.raises(Exception, match="personal"):
        check_placement(tree, "col-a", "col-me-0123")


# ── who reads what ────────────────────────────────────────────────────────


@pytest.fixture
def org_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The org's tree, groups and grant maps, served instead of ClickHouse."""
    state: dict[str, Any] = {
        "tree": sharing.Tree(parent={"col-q3": "", "col-q3-docs": "col-q3", "col-hr": ""}),
        "groups": {"user:alice": ["group:finance"]},
        "grants": {},
    }

    async def _tree_of(org: str) -> sharing.Tree:
        return state["tree"]

    async def _groups(org: str, principal: str) -> list[str]:
        return state["groups"].get(principal, [])

    async def _map(org: str, subject: str) -> GrantMap:
        return GrantMap(ranks=state["grants"].get(subject, {}))

    monkeypatch.setattr(sharing, "tree", _tree_of)
    monkeypatch.setattr(sharing, "groups_of", _groups)
    monkeypatch.setattr(grant_cache, "grant_map", _map)
    return state


def _user(name: str, agent: str = "") -> AuthInfo:
    return verify_token(_token(name, agent=agent))


def _shared(auth: AuthInfo) -> list[str]:
    """The readable collections besides the user's own personal one."""
    return [ns for ns in asyncio.run(sharing.readable(auth, ORG)) if not sharing.is_personal(ns)]


def _role(auth: AuthInfo, ns: str) -> Role | None:
    return asyncio.run(sharing.role(auth, ORG, ns))


def test_a_group_grant_on_a_parent_reaches_its_child(org_state: dict[str, Any]) -> None:
    org_state["grants"] = {"group:finance": {"col-q3": 1}}
    assert _role(_user("alice"), "col-q3-docs") is Role.VIEWER
    assert _role(_user("bob"), "col-q3-docs") is None
    assert _shared(_user("alice")) == ["col-q3", "col-q3-docs"]


def test_the_best_role_of_every_principal_wins(org_state: dict[str, Any]) -> None:
    org_state["grants"] = {"group:finance": {"col-q3": 1}, "user:alice": {"col-q3-docs": 2}}
    assert _role(_user("alice"), "col-q3-docs") is Role.WRITER


def test_an_agent_grant_counts_only_when_the_token_signs_the_agent_in(
    org_state: dict[str, Any],
) -> None:
    org_state["grants"] = {"agent:helper": {"col-hr": 1}}
    assert _role(_user("bob", "helper"), "col-hr") is Role.VIEWER
    assert _role(_user("bob"), "col-hr") is None


def test_a_detached_child_keeps_only_its_own_grants(org_state: dict[str, Any]) -> None:
    org_state["grants"] = {"group:finance": {"col-q3": 1}}
    org_state["tree"] = sharing.Tree(parent={"col-q3": "", "col-q3-docs": "", "col-hr": ""})
    assert _role(_user("alice"), "col-q3-docs") is None


def test_a_removed_member_reads_nothing_through_the_group(org_state: dict[str, Any]) -> None:
    org_state["grants"] = {"group:finance": {"col-q3": 1}}
    org_state["groups"] = {}
    assert _role(_user("alice"), "col-q3") is None
    assert _shared(_user("alice")) == []


def test_another_org_s_token_reaches_no_collection(org_state: dict[str, Any]) -> None:
    org_state["grants"] = {"user:alice": {"col-q3": 3}}
    alice_elsewhere = verify_token(end_user_issuer.mint("globex", "alice", private_pem=_PRIVATE))
    with pytest.raises(PermissionError) as refused:
        asyncio.run(sharing.check(alice_elsewhere, Role.VIEWER, ORG, "col-q3"))
    assert "collection.list" not in str(refused.value), (
        "another org's caller gets the short refusal"
    )
    assert _shared(alice_elsewhere) == []


def test_an_org_admin_without_a_grant_reads_no_collection(org_state: dict[str, Any]) -> None:
    admin = AuthInfo(
        subject="root@acme.io",
        org=ORG,
        namespaces=("*",),
        roles=(Role.ORG_ADMIN,),
        is_service_account=False,
    )
    with pytest.raises(PermissionError):
        asyncio.run(sharing.check(admin, Role.VIEWER, ORG, "col-hr"))


def test_a_refusal_points_at_the_library_and_confirms_nothing(
    org_state: dict[str, Any],
) -> None:
    org_state["tree"] = sharing.Tree(parent={"col-hr": ""})
    messages = []
    for name in ("col-hr", "col-never-made"):
        with pytest.raises(PermissionError) as refused:
            asyncio.run(sharing.check(_user("alice"), Role.VIEWER, ORG, name))
        messages.append(str(refused.value).replace(name, "<name>"))
    assert "a grant of at least viewer" in messages[0]
    # No tool route: the gate knows no transport (an assistant connection has no loom_op).
    assert "collection.list (or the dashboard's Library) lists the ones you can read" in messages[0]
    assert messages[0] == messages[1], "an existing and a missing collection read the same"
    # Someone else's personal collection takes no grant: no advice to ask for one.
    with pytest.raises(PermissionError) as refused:
        asyncio.run(sharing.check(_user("alice"), Role.VIEWER, ORG, "col-me-0123456789abcdef"))
    assert "collection.list" not in str(refused.value)
    assert "grant of at least" not in str(refused.value)


def test_a_token_is_named_in_the_refusal_only_when_its_claim_is_what_caps_it(
    org_state: dict[str, Any],
) -> None:
    org_state["tree"] = sharing.Tree(parent={"col-hr": ""})

    def refusal(auth: AuthInfo, role: Role = Role.VIEWER, grant: int = 0) -> str:
        """The refusal, with `grant` (a rank) held on col-hr by the token's own subject."""
        org_state["grants"] = {auth.subject: {"col-hr": grant}} if grant else {}
        with pytest.raises(PermissionError) as refused:
            asyncio.run(sharing.check(auth, role, ORG, "col-hr"))
        return str(refused.value)

    scoped = AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=("col-hr",),
        roles=(Role.VIEWER,),
        is_service_account=False,
        ns_roles=(("col-hr", "viewer"),),
    )
    # The claim already names it with viewer: the missing piece is the grant.
    assert "claim must name it" not in refusal(scoped)
    # A write the claim's viewer role caps: both needs are named, the claim and the grant a
    # new token is capped to, in the same words whatever the grants hold (they are never
    # read for the words: they would confirm a collection the token cannot see).
    capped = refusal(scoped, Role.WRITER, grant=2)
    assert "claim must name it with at least writer" in capped
    # The grant is read for the token's own subject, so it is the one named.
    assert "'alice' must hold a grant of at least writer" in capped
    assert refusal(scoped, Role.WRITER, grant=1) == capped
    delegated = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=True,
        creator="alice",
    )
    assert "its creator must hold at least viewer there too" in refusal(delegated)
    # A delegated claim of `*` does not name it: the claim is what caps it.
    assert "claim must name it" in refusal(delegated, grant=1)
    named = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("col-hr",),
        roles=(Role.WRITER,),
        is_service_account=True,
        creator="alice",
    )
    assert "claim must name it" not in refusal(named)
    # A delegated token whose own role is short of the write: its claim is the cap.
    viewer_bot = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("col-hr",),
        roles=(Role.VIEWER,),
        is_service_account=True,
        creator="alice",
    )
    assert "this token's claim must carry at least writer" in refusal(viewer_bot, Role.WRITER, 2)
    # A scoped writer whose ns_write leaves the collection out: its write set is the cap, and
    # its claim, which already names the collection as writer, is not blamed.
    scoped_writer = AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=("col-hr", "n"),
        roles=(Role.WRITER,),
        is_service_account=False,
        ns_roles=(("col-hr", "writer"), ("n", "writer")),
        write_namespaces=("n",),
    )
    text = refusal(scoped_writer, Role.WRITER, grant=2)
    assert "this token's write set (ns_write) must include it" in text
    assert "claim must name it" not in text
    # A claim that names the collection with a role no grant carries: the role is the fix.
    odd_role = AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=("col-hr",),
        roles=(Role.VIEWER,),
        is_service_account=False,
        ns_roles=(("col-hr", "org_admin"),),
    )
    assert "this token's claim must name it with at least viewer" in refusal(odd_role, grant=1)
    # Blocked twice (a write set and a claim that leave it out): both are named at once.
    scoped_bot = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("ns1",),
        roles=(Role.WRITER,),
        is_service_account=True,
        ns_roles=(("ns1", "writer"),),
        write_namespaces=("ns1",),
    )
    text = refusal(scoped_bot, Role.WRITER, grant=2)
    assert "this token's write set (ns_write) must include it" in text
    assert "this token's claim must name it" in text


# ── rows ──────────────────────────────────────────────────────────────────


def test_each_new_table_s_builder_writes_every_column() -> None:
    assert sorted(watermark_row(ORG)) == sorted(column_names("acl_versions"))
    assert list(COLLECTION_COLUMNS) == column_names("collections")
    assert sorted(collection_row(ORG, "col-q3")) == sorted(column_names("collections"))
    assert list(GROUP_COLUMNS) == column_names("principal_groups")
    row = group_row(ORG, "finance", display_name="Finance", external_id="", members=["user:b"])
    assert sorted(row) == sorted(column_names("principal_groups"))


def test_the_tree_is_read_from_loom_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
            seen.append(sql)
            return []

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    asyncio.run(sharing.tree_rows(ORG))
    assert "FROM loom.collections FINAL" in seen[0] and "NOT deleted" in seen[0]


def test_a_knowledge_search_reads_up_to_its_own_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import fanout
    from server.settings import settings

    names = tuple(f"col-{i}" for i in range(50))
    monkeypatch.setattr(settings, "knowledge_search_max_namespaces", 40)
    wide = fanout.Fanout(org=ORG, namespaces=names, cap=settings.knowledge_search_max_namespaces)
    read, skipped = wide.split("col-0")
    assert len(read) == 41 and len(skipped) == 9
    assert settings.memory_search_fanout_kinds == "memory", "memory search sweeps no collection"
    assert {"collection", "personal"} <= set(settings.knowledge_search_fanout_kinds.split(","))


# ── personal collections, scoped and delegated tokens ─────────────────────


def test_a_personal_collection_is_its_users_alone_and_takes_no_grant(
    org_state: dict[str, Any],
) -> None:
    alices = personal_name(ORG, "user:alice")
    org_state["grants"] = {"user:bob": {alices: 3}, "group:finance": {alices: 3}}
    assert _role(_user("alice"), alices) is Role.ADMIN
    assert _role(_user("bob"), alices) is None, "a stray grant row opens nothing"
    assert alices in asyncio.run(sharing.readable(_user("alice"), ORG))
    assert alices not in asyncio.run(sharing.readable(_user("bob"), ORG))
    assert personal_name(ORG, "user:alice") != personal_name("globex", "user:alice")


def _person(**kw: Any) -> AuthInfo:
    base: dict[str, Any] = {
        "subject": "sa:ci",
        "org": ORG,
        "namespaces": (),
        "roles": (),
        "is_service_account": True,
    }
    base.update(kw)
    return AuthInfo(**base)


def test_a_scoped_token_reaches_a_collection_no_further_than_its_claim(
    org_state: dict[str, Any],
) -> None:
    org_state["grants"] = {"sa:ci": {"col-q3": 2}}
    unnamed = _person(namespaces=("ns-a",), ns_roles=(("ns-a", "viewer"),))
    assert _role(unnamed, "col-q3") is None
    named = _person(namespaces=("col-q3",), ns_roles=(("col-q3", "viewer"),))
    assert _role(named, "col-q3") is Role.VIEWER, "capped at the claim's viewer"


@pytest.mark.parametrize(
    "token",
    [
        {"ns_roles": (("col-q3", "writer"), ("ns-a", "writer"))},
        {"roles": (Role.WRITER,)},
        {"roles": (Role.WRITER,), "creator": "ann@acme.io"},
    ],
    ids=["scoped", "flat", "delegated"],
)
def test_a_write_outside_a_token_s_ns_write_is_refused_on_a_collection_as_anywhere(
    org_state: dict[str, Any], token: dict[str, Any]
) -> None:
    """A writer token whose `ns_write` names another namespace writes neither
    an ordinary namespace nor a collection; it still reads both."""
    from server.auth.authz import _scoped_role

    org_state["grants"] = {"sa:ci": {"col-q3": 2}, "ann@acme.io": {"col-q3": 2}}
    auth = _person(namespaces=("col-q3", "ns-a"), write_namespaces=("ns-b",), **token)
    assert _role(auth, "col-q3") is Role.VIEWER
    if auth.has_ns_roles():
        assert _scoped_role(auth, Role.WRITER, ORG, "ns-a", cap=None) is None
        assert _scoped_role(auth, Role.VIEWER, ORG, "ns-a", cap=None) is Role.WRITER
    writable = _person(namespaces=("col-q3",), write_namespaces=("col-q3",), **token)
    assert _role(writable, "col-q3") is Role.WRITER


def test_a_delegated_token_reaches_a_collection_no_further_than_its_creator(
    org_state: dict[str, Any],
) -> None:
    org_state["grants"] = {"sa:ci": {"col-q3": 2}, "ann@acme.io": {"col-q3": 1}}
    token = _person(namespaces=("col-q3",), roles=(Role.WRITER,), creator="ann@acme.io")
    assert _role(token, "col-q3") is Role.VIEWER
    org_state["grants"] = {"sa:ci": {"col-q3": 2}}
    assert _role(token, "col-q3") is None, "the creator holds nothing there"


def test_a_trusted_caller_cannot_become_a_grant_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "trusted_callers", "sa:proxy")
    proxy = _person(subject="sa:proxy", namespaces=("*",), roles=(Role.WRITER,))
    headers = ("user:u-1", "group:finance", "agent:helper", "everyone")
    for header in (*headers, "acme/user:u-1", "team/everyone"):
        with pytest.raises(PermissionError):
            gateway.effective_auth(proxy, header)
    assert gateway.effective_auth(proxy, "bob@acme.io").subject == "bob@acme.io"


def test_an_end_user_gets_no_directory_self_service() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates

    end_user = _user("alice")
    for name in ("namespace.register", "namespace.join", "namespace.browse"):
        op = OPERATIONS_BY_NAME[name]
        target = gates.Target(
            org=ORG, namespace=None, named_namespace=None, register_namespace=None
        )
        with pytest.raises(PermissionError, match="not open to an end user"):
            asyncio.run(gates.authorize(op, end_user, target, transport="http"))
    assert {"collection.create", "collection.list"} <= gates.END_USER_ORG_OPS


def test_an_erasure_rewrite_outranks_a_row_versioned_by_a_faster_clock() -> None:
    from server.erasure.store import steps_principals

    assert steps_principals._NEW_VERSION.startswith("greatest(version + 1,")


# ── principals, erasure and the transports ───────────────────────────────


@pytest.mark.parametrize("header", ["User:alice", "GROUP:finance", "Agent:helper"])
def test_a_principal_spelled_in_another_case_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "trusted_callers", "sa:proxy")
    proxy = _person(subject="sa:proxy", namespaces=("*",), roles=(Role.WRITER,))
    with pytest.raises(PermissionError):
        gateway.effective_auth(proxy, header)
    with pytest.raises(ValueError, match="grant principal"):
        mint_token(header, ORG, ["team"], ["viewer"], secret=SECRET)


def test_a_token_whose_subject_is_a_principal_does_not_verify() -> None:
    import jwt as pyjwt

    from server.settings import settings

    forged = pyjwt.encode(
        {
            "sub": "user:alice",
            "org": ORG,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": 1,
        },
        SECRET,
        algorithm="HS256",
        headers={"kid": "override"},
    )
    with pytest.raises(TokenError, match="grant principal"):
        verify_token(forged, secret=SECRET)


def test_a_collection_outside_the_tree_admits_no_one(org_state: dict[str, Any]) -> None:
    org_state["grants"] = {"user:alice": {"col-gone": 3, "col-q3": 3}}
    assert _role(_user("alice"), "col-gone") is None, "deleted, or a grant planted on a name"
    assert _role(_user("alice"), "col-q3") is Role.ADMIN
    roles = asyncio.run(sharing.roles(_user("alice"), ORG, ["col-gone", "col-q3"]))
    assert roles == {"col-gone": None, "col-q3": Role.ADMIN}


def test_a_collection_is_never_adopted() -> None:
    from server.identity.app.grants import adopt_namespace

    with pytest.raises(Exception, match="not adopted"):
        asyncio.run(adopt_namespace(ORG, "col-hr", "root@acme.io", actor="root@acme.io"))


def test_an_end_user_does_not_read_an_org_readable_code_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz

    async def _readable(org: str, namespace: str) -> bool:
        return True

    async def _not_restricted(org: str, namespace: str) -> bool:
        return False

    monkeypatch.setattr(authz, "is_org_readable_code_namespace", _readable)
    monkeypatch.setattr(authz, "may_be_restricted", _not_restricted)
    member = _person(subject="ann@acme.io", is_service_account=False, roles=(Role.VIEWER,))
    asyncio.run(authz.check_authorized(member, Role.VIEWER, ORG, "code-acme"))
    with pytest.raises(PermissionError):
        asyncio.run(authz.check_authorized(_user("alice"), Role.VIEWER, ORG, "code-acme"))


@pytest.mark.parametrize(
    "subject", ["Group:Eng", "AGENT:helper", "User:Bob", "team/everyone", "acme/user:u-1"]
)
def test_a_grant_to_a_principal_in_another_case_is_refused(subject: str) -> None:
    """In another case, or after a `/` (the shape of an end user's revocation
    key): no token carries such a subject, so no grant names one."""
    from server.core.errors import ValidationError
    from server.identity.app.grants import _enforce_principal_target

    with pytest.raises(ValidationError, match="well-formed principal"):
        _enforce_principal_target("col-hr", subject)


@pytest.mark.parametrize(
    "auth",
    [
        None,
        AuthInfo(
            subject="root",
            org="*",
            namespaces=("*",),
            roles=(Role.PLATFORM_ADMIN,),
            is_service_account=False,
        ),
    ],
)
def test_no_collection_is_hidden_with_auth_off_or_from_a_platform_admin(
    auth: AuthInfo | None,
) -> None:
    from server.auth import context

    async def _clause() -> tuple[str, dict[str, Any]]:
        context.set_current_auth(auth)
        return await context.collection_clause(ORG)

    assert asyncio.run(_clause()) == ("", {})


def test_a_document_rewrite_keeps_who_wrote_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A metadata edit or a delete writes the next version from the row read."""
    from server.knowledge.store import documents

    stored = {
        "org": ORG,
        "namespace": "col-hr",
        "source_uri": "doc/1",
        "doc_id": "d1",
        "content": "text",
        "version": 1,
        "ingested_at": datetime(2026, 1, 1),
        "agent": "helper",
        "user_id": "bob",
        "actor": "user:bob",
        "source_class": "asserted",
    }

    class _CH:
        async def query_all(self, sql: str, params: Any = None, **_k: Any) -> list[dict[str, Any]]:
            assert all(c in sql for c in ("agent", "user_id", "actor", "source_class"))
            return [stored]

    row = asyncio.run(documents.get_one(ORG, "col-hr", "doc/1", ch=_CH()))  # type: ignore[arg-type]
    rewritten = documents.build_document_row(row, title="new", version=2)
    assert {k: rewritten[k] for k in ("agent", "user_id", "actor", "source_class")} == {  # type: ignore[literal-required]
        "agent": "helper",
        "user_id": "bob",
        "actor": "user:bob",
        "source_class": "asserted",
    }


@pytest.mark.parametrize("value", ["-", "", " ", "*"])
def test_an_erasure_or_export_of_no_user_is_refused(value: str) -> None:
    from pydantic import ValidationError

    from server.erasure.api.erasure import ErasureExportInput, ErasureRequestInput

    scope = {"org": ORG, "scope_type": "user_id", "scope_value": value, "requester": "ops"}
    with pytest.raises(ValidationError, match="names no user"):
        ErasureExportInput(**scope)
    with pytest.raises(ValidationError, match="names no user"):
        ErasureRequestInput(**scope)


def test_an_end_user_token_is_served_only_where_end_users_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The op endpoint and downloads serve it; `/v1/me/*`, the admin routes and
    the task watch refuse it (403): an end user has no account there."""
    from fastapi import HTTPException

    from server.auth import bearer

    monkeypatch.setattr(bearer, "verify_bearer", lambda _token: _user("alice"))
    with pytest.raises(HTTPException) as refused:
        bearer.verify_bearer_header("Bearer eu")
    assert refused.value.status_code == 403
    assert bearer.verify_bearer_header("Bearer eu", acts_for_end_users=True) is not None


def test_the_op_endpoint_runs_an_end_user_token_as_its_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.auth import bearer
    from server.transport import http

    monkeypatch.setattr(bearer, "verify_bearer", lambda _token: _user("alice", "helper"))
    auth = http._extract_auth(SimpleNamespace(headers={"Authorization": "Bearer eu"}))  # type: ignore[arg-type]
    assert auth is not None and (auth.subject, auth.agent_principal) == (
        "user:alice",
        "agent:helper",
    )


def test_a_tree_or_group_row_outranks_the_row_it_replaces_whatever_the_clock() -> None:
    ahead = 2**62  # a row written from a host whose clock ran far ahead
    assert collection_row(ORG, "col-q3", deleted=True, after=ahead)["version"] > ahead
    assert (
        group_row(ORG, "g", display_name="", external_id="", members=[], after=ahead)["version"]
        > ahead
    )


def test_a_collection_delete_cut_short_is_closed_by_its_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tree is retired on every delete, after the directory row: a retry
    after a failed retire finds the row inactive and still closes the tree."""
    from contextlib import asynccontextmanager

    from server.identity.app import collections, namespace

    directory = {
        "org": ORG,
        "namespace": "col-hr",
        "kind": "collection",
        "active": True,
        "updated_at": datetime(2026, 1, 1),
    }
    retired: list[str] = []

    @asynccontextmanager
    async def _claim(*_a: Any) -> Any:
        yield

    async def _row(*_a: Any, **_k: Any) -> dict[str, Any]:
        return dict(directory)

    async def _retire(org: str, ns: str) -> None:
        if not retired:
            retired.append("failed")
            raise RuntimeError("claim busy")
        retired.append(ns)

    class _CH:
        async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
            directory.update(active=rows[0]["active"])

    async def _changed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(namespace, "account_change", _claim)
    monkeypatch.setattr(namespace, "get_client", lambda: _CH())
    monkeypatch.setattr(namespace.namespaces_repo, "fetch_row", _row)
    monkeypatch.setattr(namespace.grant_cache, "changed", _changed)
    monkeypatch.setattr(collections, "retire", _retire)
    with pytest.raises(RuntimeError):
        asyncio.run(namespace.delete_namespace(ORG, "col-hr"))
    assert directory["active"] is False and retired == ["failed"], "the tree still stands"
    asyncio.run(namespace.delete_namespace(ORG, "col-hr"))
    assert retired == ["failed", "col-hr"], "the retry closes it"


def test_a_grant_to_a_group_is_written_under_the_groups_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SCIM delete holds the same claim, so it cannot pass between the check
    that the group exists and the grant's write."""
    from contextlib import asynccontextmanager

    from server.core.errors import ValidationError
    from server.identity.app import grants, scim_groups

    held: list[str] = []
    events: list[str] = []

    @asynccontextmanager
    async def _group_change(org: str, group_id: str) -> Any:
        held.append(group_id)
        yield
        held.remove(group_id)

    async def _fetch(org: str, group_id: str) -> dict[str, Any] | None:
        events.append(f"check {group_id} held={held}")
        return None if group_id == "gone" else {"group_id": group_id}

    async def _create(_input: Any) -> Any:
        events.append(f"write held={held}")
        return None

    monkeypatch.setattr(grants, "group_change", _group_change)
    monkeypatch.setattr(scim_groups, "fetch_group", _fetch)
    monkeypatch.setattr(grants, "_create_grant", _create)

    async def _run(subject: str) -> None:
        async with grants._group_held(ORG, subject):
            await grants._create_grant(None)

    asyncio.run(_run("group:eng"))
    assert events == ["check eng held=['eng']", "write held=['eng']"]
    with pytest.raises(ValidationError, match="no group"):
        asyncio.run(_run("group:gone"))


def test_mcp_refuses_an_end_user_token_as_not_allowed_not_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fastapi import Response

    from server.auth import jwt
    from server.transport import mcp_auth

    monkeypatch.setattr(jwt, "verify_token", lambda _token: _user("alice"))
    request = SimpleNamespace(headers={"authorization": "Bearer eu"})
    with pytest.raises(PermissionError) as refused:
        mcp_auth._verify_bearer(request)  # type: ignore[arg-type]
    response = Response()
    answer = mcp_auth._not_on_mcp(response, {"id": 7}, refused.value)
    assert response.status_code == 403 and answer["id"] == 7


@pytest.mark.parametrize("auth", [None, "writer"])
def test_the_gate_never_takes_me_as_a_namespace_name(auth: str | None) -> None:
    """HTTP resolves `@me` first; a call that reaches the gate with it (MCP)
    is refused, so nothing addresses or registers a namespace called `@me`."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates

    op = OPERATIONS_BY_NAME["knowledge.add"]
    who = (
        None if auth is None else _person(subject="a@x.io", namespaces=("*",), roles=(Role.WRITER,))
    )
    target = gates.authz_target(op, org=ORG, namespace="@me", auth=who)
    with pytest.raises(PermissionError, match="personal collection"):
        asyncio.run(gates.authorize(op, who, target, transport="mcp"))


def test_a_write_of_the_same_text_keeps_its_author_and_new_text_takes_the_writer() -> None:
    from server.knowledge.app.writes import _author_for
    from server.knowledge.store.documents import DocumentRow

    bobs = DocumentRow(
        org=ORG,
        namespace="col-hr",
        source_uri="doc/1",
        doc_id="d",
        content="plan",
        version=1,
        ingested_at=datetime(2026, 1, 1),
        agent="-",
        user_id="bob",
        actor="user:bob",
        source_class="asserted",
    )
    alice = {"agent": "-", "user_id": "alice", "actor": "user:alice", "source_class": "asserted"}
    assert _author_for(bobs, "plan", alice)["user_id"] == "bob"  # a re-save, a tag edit
    assert _author_for(bobs, "new plan", alice) == alice
    gone = bobs.model_copy(update={"superseded_at": datetime(2026, 1, 2)})
    assert _author_for(gone, "plan", alice) == alice, "a deleted document's text is written anew"
    assert _author_for(None, "plan", alice) == alice


@pytest.mark.parametrize("op_name", ["collection.list", "collection.create"])
def test_a_scoped_token_may_list_and_create_collections(op_name: str) -> None:
    """Neither op names a namespace to gate; a scoped token is admitted on its
    best namespace role rather than refused as IDENTITY_INCOMPLETE."""
    from server.auth import authz
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME[op_name]
    scoped = _person(namespaces=("col-q3",), ns_roles=(("col-q3", "viewer"),))
    asyncio.run(
        authz.check_authorized(
            scoped, op.required_role, org=ORG, namespace=None, **authz.scope_flags(op)
        )
    )


class _GrantRows:
    """loom.namespace_grants as revoke_subject and forget_granter read it: the
    newest row per (namespace, subject); `sticky` keeps writes from landing."""

    def __init__(self, *rows: dict[str, Any], sticky: bool = False) -> None:
        self.rows = {(r["namespace"], r["subject"]): r for r in rows}
        self.written: list[dict[str, Any]] = []
        self.sticky = sticky
        self.moved: list[list[str]] = []
        self.audited: list[str] = []

    async def live_rows_of(self, org: str, subject: str, **_k: Any) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["subject"] == subject and not r["revoked_at"]]

    async def newest_row(self, org: str, ns: str, subject: str, **_k: Any) -> dict | None:
        return self.rows.get((ns, subject))

    async def rows_granted_by(self, org: str, granter: str, **_k: Any) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["granted_by"] == granter]

    async def insert_row(self, row: dict[str, Any], **_k: Any) -> None:
        self.written.append(row)
        if not self.sticky:
            self.rows[(row["namespace"], row["subject"])] = row


@pytest.fixture
def grant_rows(monkeypatch: pytest.MonkeyPatch) -> Any:
    from contextlib import asynccontextmanager

    from server.identity.app import grants

    def _install(*rows: dict[str, Any], sticky: bool = False) -> _GrantRows:
        store = _GrantRows(*rows, sticky=sticky)
        for name in ("live_rows_of", "newest_row", "rows_granted_by", "insert_row"):
            monkeypatch.setattr(grants.grants_repo, name, getattr(store, name))

        @asynccontextmanager
        async def _claim(*_a: Any) -> Any:
            yield

        async def _changed(org: str, subjects: Any = None, **_k: Any) -> None:
            store.moved.append(list(subjects or []))

        async def _emit(event: str, *_a: Any, **_k: Any) -> str:
            store.audited.append(event)
            return "h"

        monkeypatch.setattr(grants, "grant_change", _claim)
        monkeypatch.setattr(grants.grant_cache, "changed", _changed)
        monkeypatch.setattr("server.observability.audit.emit", _emit)
        return store

    return _install


def _grant_row(subject: str, **over: Any) -> dict[str, Any]:
    row = {"org": ORG, "namespace": "col-hr", "subject": subject, "version": 1}
    row.update({"revoked_at": None, "granted_by": "user:alice", **over})
    return row


def test_a_revoke_outranks_a_grant_versioned_by_a_faster_clock(grant_rows: Any) -> None:
    from server.identity.app.grants import revoke_subject

    store = grant_rows(_grant_row("user:bob", version=2**62))
    assert asyncio.run(revoke_subject(ORG, "user:bob", actor="ops")) == 1
    assert store.written[0]["version"] > 2**62 and store.written[0]["revoked_at"] is not None
    assert store.moved == [["user:bob"]], "the watermark moves once"


def test_a_grant_that_stays_live_after_its_revoke_fails_the_call(grant_rows: Any) -> None:
    from server.core.errors import BackendUnavailableError
    from server.identity.app.grants import revoke_subject

    grant_rows(_grant_row("user:bob"), sticky=True)  # every read finds it live again
    with pytest.raises(BackendUnavailableError, match="still live"):
        asyncio.run(revoke_subject(ORG, "user:bob", actor="ops"))


@pytest.mark.parametrize("call", ["revoke", "forget"])
def test_a_write_whose_reply_is_lost_still_moves_the_watermark(
    grant_rows: Any, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """The row may have landed; a retry finds nothing left to do and would
    announce nothing, so the failed call announces it."""
    from server.identity.app import grants

    store = grant_rows(_grant_row("user:bob", granted_by="user:bob"))

    async def _lost(row: dict[str, Any], **_k: Any) -> None:
        store.written.append(row)
        raise TimeoutError("reply lost")

    monkeypatch.setattr(grants.grants_repo, "insert_row", _lost)
    with pytest.raises(TimeoutError):
        if call == "revoke":
            asyncio.run(grants.revoke_subject(ORG, "user:bob", actor="ops"))
        else:
            asyncio.run(grants.forget_granter(ORG, "user:bob"))
    assert len(store.written) == 1 and store.moved == [["user:bob"]]
    if call == "revoke":
        assert store.audited == ["grants.revoke"], "a revoke that may have landed is audited"


def test_an_erased_user_is_blanked_as_the_issuer_of_grants_others_hold(grant_rows: Any) -> None:
    from server.identity.app import grants

    store = grant_rows(_grant_row("user:carol", version=5, granted_by="user:bob"))
    assert asyncio.run(grants.forget_granter(ORG, "user:bob")) == 1
    [row] = store.written
    assert (row["granted_by"], row["subject"], row["revoked_at"]) == ("", "user:carol", None)
    assert row["version"] > 5


def test_blanking_the_issuer_keeps_a_revoke_that_landed_meanwhile(
    grant_rows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is read again under its claim: a revoke written after the list
    read is carried, never undone by the rewrite."""
    from server.identity.app import grants

    listed = _grant_row("user:carol", version=5, granted_by="user:bob")
    store = grant_rows(listed)
    revoked = {**listed, "revoked_at": datetime(2026, 1, 2), "version": 6}

    async def _listed(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        store.rows[("col-hr", "user:carol")] = revoked  # the revoke lands after this read
        return [listed]

    monkeypatch.setattr(grants.grants_repo, "rows_granted_by", _listed)
    asyncio.run(grants.forget_granter(ORG, "user:bob"))
    [row] = store.written
    assert row["revoked_at"] == datetime(2026, 1, 2) and row["version"] > 6


def test_a_personal_collection_is_not_deleted_as_a_namespace() -> None:
    from server.core.errors import ValidationError
    from server.identity.api.models import NamespaceDeleteInput
    from server.identity.app.namespace import handle_namespace_delete

    mine = NamespaceDeleteInput(org=ORG, namespace=personal_name(ORG, "user:bob"))
    with pytest.raises(ValidationError, match="erasure"):
        asyncio.run(handle_namespace_delete(mine))


@pytest.mark.parametrize("read", ["tree", "groups_of"])
@pytest.mark.parametrize(
    "error", ["BackendUnavailableError", "BackendTimeoutError", "OverloadedError"]
)
def test_an_outage_answers_503_for_a_collection_as_for_any_namespace(
    monkeypatch: pytest.MonkeyPatch, org_state: dict[str, Any], read: str, error: str
) -> None:
    """A service in front of Loom must tell an outage from a revoke: when the
    tree or the groups cannot be read now (down, past the deadline, out of
    connections), the collection gate and the readable set raise the 503,
    never a 403 or an empty list."""
    from server.core import errors

    failure = getattr(errors, error)

    async def _down(*_a: Any) -> Any:
        raise failure("unreadable")

    monkeypatch.setattr(sharing, read, _down)
    user = _user("alice")
    with pytest.raises(failure):
        asyncio.run(sharing.check(user, Role.VIEWER, ORG, "col-hr"))
    with pytest.raises(failure):
        asyncio.run(sharing.readable(user, ORG))


def test_an_unreadable_grant_refuses_with_the_short_words(
    monkeypatch: pytest.MonkeyPatch, org_state: dict[str, Any]
) -> None:
    """A tree that cannot be read refuses (fail closed), but asks for no grant: the caller
    may already hold one, and should retry rather than ask for it."""

    async def _broken(*_a: Any) -> Any:
        raise RuntimeError("corrupt")

    monkeypatch.setattr(sharing, "tree", _broken)
    with pytest.raises(PermissionError) as refused:
        asyncio.run(sharing.check(_user("alice"), Role.WRITER, ORG, "col-hr"))
    assert str(refused.value) == f"principal 'user:alice' cannot access {ORG}/col-hr"


def test_every_op_an_end_user_reaches_without_a_namespace_is_known() -> None:
    """An op that names no namespace and is not org-level passes the role gate
    on the end user's org membership alone: memory.get_by_id authorizes its
    row's namespace, and erasure.status is refused by the end user's own-user
    read scope. A new one must say which it does."""
    from server.operations.registry import ALL_OPERATIONS

    reachable = sorted(
        o.name
        for o in ALL_OPERATIONS
        if o.required_role.value <= Role.VIEWER.value
        and not (o.skip_namespace_authz or o.deployment_scoped)
        and "namespace" not in o.input_model.model_fields
    )
    assert reachable == ["erasure.status", "memory.get_by_id"], reachable


def test_a_collection_whose_create_stopped_short_is_still_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree row with no directory row (a create cut short) still admits by
    its grants, so a delete retires it."""
    from contextlib import asynccontextmanager

    from server.core.errors import ValidationError
    from server.identity.api.models import NamespaceDeleteInput
    from server.identity.app import collections, namespace

    retired: list[str] = []

    @asynccontextmanager
    async def _claim(*_a: Any) -> Any:
        yield

    async def _no_row(*_a: Any, **_k: Any) -> None:
        return None

    async def _retire(org: str, ns: str) -> bool:
        retired.append(ns)
        return True

    monkeypatch.setattr(namespace, "account_change", _claim)
    monkeypatch.setattr(namespace, "get_client", lambda: object())
    monkeypatch.setattr(namespace.namespaces_repo, "fetch_row", _no_row)
    monkeypatch.setattr(collections, "retire", _retire)
    out = asyncio.run(namespace.delete_namespace(ORG, "col-hr"))
    assert out.deleted is True and retired == ["col-hr"]
    # Only `collection.delete`, which purges what it holds first, deletes one.
    with pytest.raises(ValidationError, match="collection.delete"):
        asyncio.run(
            namespace.handle_namespace_delete(NamespaceDeleteInput(org=ORG, namespace="col-hr"))
        )


def test_an_end_users_original_never_passes_to_another_writer_of_new_text() -> None:
    """Without fresh bytes a write carries the prior original, but not an end
    user's to someone else: that user's erasure must still find it."""
    from server.knowledge.app.writes import _original_of
    from server.knowledge.store.documents import DocumentRow

    prior = DocumentRow(
        org=ORG,
        namespace="col-hr",
        source_uri="doc/x",
        doc_id="d",
        content="a's text",
        version=1,
        ingested_at=datetime(2026, 1, 1),
        user_id="a",
        blob_path="sha-x",
    )
    b = {"agent": "-", "user_id": "b", "actor": "user:b", "source_class": "asserted"}
    assert _original_of(prior, b) is None
    assert _original_of(prior, {**b, "user_id": "a"}) is prior, "a's own edit keeps it"
    team = prior.model_copy(update={"user_id": "-"})
    assert _original_of(team, b) is team, "a document no end user wrote carries as before"


def test_a_grant_to_everyone_reaches_every_member_of_the_org(org_state: dict[str, Any]) -> None:
    """The org-wide principal: no group to keep in step with the org's users."""
    org_state["grants"] = {"everyone": {"col-hr": 1}}
    assert _role(_user("anyone"), "col-hr") is Role.VIEWER
    assert _role(_user("anyone"), "col-q3") is None


def test_everyone_holds_grants_on_collections_only_and_no_token_is_it() -> None:
    from server.core.errors import ValidationError
    from server.core.principals import is_reserved_subject
    from server.identity.app.grants import _enforce_principal_target

    _enforce_principal_target("col-hr", "everyone")
    with pytest.raises(ValidationError, match="collections only"):
        _enforce_principal_target("team", "everyone")
    assert is_reserved_subject("Everyone"), "no token, in any case, is everyone"


def test_a_search_naming_its_namespaces_reads_those_alone_and_each_must_be_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz, context
    from server.core.errors import AuthInsufficientError
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search

    user = _user("alice")
    readable = {"col-agent-helper", personal_name(ORG, "user:alice")}

    async def _check(auth: Any, role: Role, *, org: str, namespace: str, **_k: Any) -> None:
        if namespace not in readable:
            raise PermissionError(f"cannot read {namespace}")

    monkeypatch.setattr(authz, "check_authorized", _check)

    def _named(*names: str) -> list[str]:
        query = KnowledgeSearchInput(
            org=ORG, namespace="col-agent-helper", query="q", namespaces=list(names)
        )
        return asyncio.run(knowledge_search._named_namespaces(query))

    context.set_current_auth(user)
    try:
        assert _named("@me") == ["col-agent-helper", personal_name(ORG, "user:alice")]
        with pytest.raises(AuthInsufficientError):
            _named("col-agent-other")
    finally:
        context.set_current_auth(None)


@pytest.mark.parametrize(
    ("spec", "said"),
    [
        ({"loom": {"orgs": ["acme"], "keys": {"k": "pem"}}}, "both end-user and trusted"),
        ({"chat": {"orgs": ["*"], "keys": {"k": "pem"}}}, "not every org"),
        ({"chat": {"orgs": [], "keys": {"k": "pem"}}}, "lists the orgs"),
        ({"chat": {"orgs": ["acme"]}}, "jwks_url` or `keys"),
        ({"chat": {"orgs": ["acme"], "jwks_url": "http://keys"}}, "is https"),
        ({"chat": {"orgs": ["acme"], "keys": {"k": "pem"}, "roles": ["admin"]}}, "unknown fields"),
        (
            {
                "chat": {"orgs": ["acme"], "keys": {"k": "pem"}},
                "mail": {"orgs": ["ac*"], "keys": {"m": "pem"}},
            },
            "both sign for",
        ),
    ],
)
def test_an_end_user_issuer_names_its_orgs_and_its_keys(spec: dict[str, Any], said: str) -> None:
    import json

    from pydantic import ValidationError

    from server.settings.catalog import AuthSettings

    with pytest.raises(ValidationError, match=said):
        AuthSettings(jwt_end_user_issuers_json=json.dumps(spec))


def test_an_end_user_issuer_is_never_a_trusted_one_and_signs_for_its_orgs_alone() -> None:
    import json

    from pydantic import ValidationError

    from server.settings.catalog import AuthSettings

    with pytest.raises(ValidationError, match="both end-user and trusted issuers"):
        AuthSettings(
            jwt_end_user_issuers_json=json.dumps(
                {"idp": {"orgs": ["acme"], "keys": {"k": _PUBLIC}}}
            ),
            jwt_trusted_issuers="idp",
        )
    issuers = AuthSettings(
        jwt_end_user_issuers_json=end_user_issuer.issuers_setting(_PUBLIC)
    ).end_user_issuers()
    spec = issuers[end_user_issuer.ISSUER]
    assert spec.keys == {end_user_issuer.KID: _PUBLIC}
    assert spec.signs_for("acme") and spec.signs_for("it-share-1a2b")
    assert not spec.signs_for("initech") and not spec.signs_for("itx")


def test_an_end_user_token_of_an_org_its_issuer_does_not_sign_for_is_refused() -> None:
    with pytest.raises(TokenError, match="does not sign for"):
        verify_token(_token(org="initech"))


def test_an_end_user_token_carries_a_jti() -> None:
    import time

    import jwt as pyjwt

    from server.settings import settings

    now = int(time.time())
    claims = {"iss": end_user_issuer.ISSUER, "aud": settings.jwt_audience, "sub": "user:u-1"}
    claims.update({"org": ORG, "iat": now, "exp": now + 60})
    token = pyjwt.encode(claims, _PRIVATE, algorithm="RS256", headers={"kid": end_user_issuer.KID})
    with pytest.raises(TokenError, match="jti"):
        verify_token(token)


def test_an_end_user_issuer_s_keys_come_from_its_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Until the first fetch of its JWKS lands, its tokens are answered retry
    (503); a fetched key verifies them."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from server.auth import end_user_keys
    from server.auth.jwt import AuthUnavailable
    from server.settings import settings

    monkeypatch.setattr(
        settings,
        "jwt_end_user_issuers_json",
        end_user_issuer.issuers_setting(_PUBLIC, keys={}, jwks_url="https://chat.example/jwks"),
    )
    end_user_keys.clear()
    try:
        with pytest.raises(AuthUnavailable):
            verify_token(_token())
        end_user_keys._keys[end_user_issuer.ISSUER] = {
            end_user_issuer.KID: load_pem_public_key(_PUBLIC.encode())
        }
        assert verify_token(_token()).subject == "user:u-1"
    finally:
        end_user_keys.clear()


def test_an_end_user_issuer_s_jwks_is_fetched_and_a_failed_fetch_keeps_the_last_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import base64

    import httpx
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from server.auth import end_user_keys
    from server.settings import settings

    numbers = load_pem_public_key(_PUBLIC.encode()).public_numbers()  # type: ignore[union-attr]

    def b64(n: int) -> str:
        return (
            base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big"))
            .rstrip(b"=")
            .decode()
        )

    jwk = {
        "kty": "RSA",
        "kid": end_user_issuer.KID,
        "use": "sig",
        "n": b64(numbers.n),
        "e": b64(numbers.e),
    }
    answers = [httpx.Response(200, json={"keys": [jwk]}), httpx.Response(500)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://chat.example/jwks"
        return answers.pop(0)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(
        settings,
        "jwt_end_user_issuers_json",
        end_user_issuer.issuers_setting(_PUBLIC, keys={}, jwks_url="https://chat.example/jwks"),
    )
    end_user_keys.clear()
    try:
        asyncio.run(end_user_keys.refresh())
        assert end_user_keys.key(end_user_issuer.ISSUER, end_user_issuer.KID) is not None
        asyncio.run(end_user_keys.refresh())
        assert end_user_keys.key(end_user_issuer.ISSUER, end_user_issuer.KID) is not None
        assert verify_token(_token()).subject == "user:u-1"
    finally:
        end_user_keys.clear()


@pytest.mark.parametrize(
    ("end_user", "asked", "read"),
    [(True, None, "this"), (True, "all_mine", "all_mine"), (False, None, "all_mine")],
)
def test_an_end_user_s_search_reads_its_namespace_unless_it_asks_for_more(
    monkeypatch: pytest.MonkeyPatch, end_user: bool, asked: str | None, read: str
) -> None:
    from server.auth import context
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search

    scopes: list[str] = []

    class _Read(Exception):
        pass

    async def _namespaces(org: str, namespace: str, scope: str, **_k: Any) -> Any:
        scopes.append(scope)
        raise _Read

    class _Embedder:
        model_name = "fake"

        async def embed(self, text: str) -> list[float]:
            return [0.0]

    monkeypatch.setattr(knowledge_search, "namespaces_to_search", _namespaces)
    monkeypatch.setattr(knowledge_search, "get_embedder", _Embedder)
    scope = {} if asked is None else {"namespace_scope": asked}
    query = KnowledgeSearchInput(org=ORG, namespace="col-team", query="q", **scope)
    member = AuthInfo(
        subject="bob@acme.io",
        org=ORG,
        namespaces=("col-team",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    context.set_current_auth(_user("alice") if end_user else member)
    try:
        with pytest.raises(_Read):
            asyncio.run(knowledge_search.handle_knowledge_search(query))
    finally:
        context.set_current_auth(None)
    assert scopes == [read]


def test_an_end_user_issuer_s_jwks_is_refetched_every_refresh_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import end_user_keys
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_end_user_jwks_refresh_seconds", 42.0)
    # Every issuer's keys are at hand, so the loop keeps the full period.
    monkeypatch.setattr(
        settings, "jwt_end_user_issuers_json", end_user_issuer.issuers_setting(_PUBLIC)
    )
    end_user_keys.clear()
    slept: list[float] = []

    class _Done(Exception):
        pass

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    async def _refresh(**_k: Any) -> None:
        if len(slept) == 2:
            raise _Done

    monkeypatch.setattr(end_user_keys.asyncio, "sleep", _sleep)
    monkeypatch.setattr(end_user_keys, "refresh", _refresh)

    async def _run() -> None:
        with pytest.raises(_Done):
            await end_user_keys.start_refresh_loop()

    asyncio.run(_run())
    assert slept == [42.0, 42.0]


def test_a_failed_first_jwks_fetch_is_retried_within_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Until an issuer's first fetch lands its tokens are refused (503), so the
    loop retries every 2 s, not every refresh period."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from server.auth import end_user_keys
    from server.settings import settings

    monkeypatch.setattr(
        settings,
        "jwt_end_user_issuers_json",
        end_user_issuer.issuers_setting(_PUBLIC, keys={}, jwks_url="https://chat.example/jwks"),
    )
    monkeypatch.setattr(settings, "jwt_end_user_jwks_refresh_seconds", 300.0)
    slept: list[float] = []
    fetches = 0
    asked: list[dict[str, Any]] = []

    class _Done(Exception):
        pass

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    async def _refresh(**_k: Any) -> None:
        nonlocal fetches
        asked.append(_k)
        fetches += 1
        if fetches == 2:  # the retry lands
            end_user_keys._keys[end_user_issuer.ISSUER] = {
                end_user_issuer.KID: load_pem_public_key(_PUBLIC.encode())
            }
        if fetches == 3:
            raise _Done

    monkeypatch.setattr(end_user_keys.asyncio, "sleep", _sleep)
    monkeypatch.setattr(end_user_keys, "refresh", _refresh)
    end_user_keys.clear()

    async def _run() -> None:
        with pytest.raises(_Done):
            await end_user_keys.start_refresh_loop()

    try:
        asyncio.run(_run())
        assert slept == [2.0, 2.0, 300.0]
        # The fast retries fetch only the issuers still unloaded.
        assert asked == [{"unloaded_only": True}, {"unloaded_only": True}, {}]
        assert verify_token(_token()).subject == "user:u-1"
    finally:
        end_user_keys.clear()


def test_a_user_in_many_groups_reaches_a_grant_of_the_last_one(org_state: dict[str, Any]) -> None:
    """Grant maps are read in batches; every principal's still counts."""
    groups = [f"group:g{i}" for i in range(12)]
    org_state["groups"] = {"user:alice": groups}
    org_state["grants"] = {groups[-1]: {"col-hr": 1}}
    assert _role(_user("alice"), "col-hr") is Role.VIEWER
    assert _shared(_user("alice")) == ["col-hr"]


def test_one_key_is_never_both_an_end_user_and_a_federated_key() -> None:
    """Compared by key, not kid: a federated key verifies Loom-shaped tokens."""
    import json

    from pydantic import ValidationError

    from server.settings.catalog import AuthSettings

    with pytest.raises(ValidationError, match="are federated keys"):
        AuthSettings(
            jwt_end_user_issuers_json=end_user_issuer.issuers_setting(_PUBLIC),
            jwt_public_keys_json=json.dumps({"gateway-1": _PUBLIC}),
        )
    other = end_user_issuer.new_key()[1]
    AuthSettings(
        jwt_end_user_issuers_json=end_user_issuer.issuers_setting(_PUBLIC),
        jwt_public_keys_json=json.dumps({"gateway-1": other}),
    )


def test_a_fetched_key_that_is_a_federated_one_is_dropped_and_issuers_fetch_side_by_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from server.auth import end_user_keys
    from server.settings import settings

    other_private, other_public = end_user_issuer.new_key()
    del other_private
    issuers = json.loads(
        end_user_issuer.issuers_setting(_PUBLIC, keys={}, jwks_url="https://a.example/jwks")
    )
    issuers["second"] = {"orgs": ["zeta"], "jwks_url": "https://b.example/jwks"}
    monkeypatch.setattr(settings, "jwt_end_user_issuers_json", json.dumps(issuers))
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"gw": other_public}))
    in_flight = peak = 0

    class _Set:
        def __init__(self, keys: list[Any]) -> None:
            self.keys = keys

    class _Key:
        def __init__(self, kid: str, pem: str) -> None:
            self.key_id, self.key = kid, load_pem_public_key(pem.encode())

    async def _fetch_json(url: str) -> dict[str, Any]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return {"url": url}

    class _Response:
        def __init__(self, url: str) -> None:
            self.url = url

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"url": self.url}

    class _Client:
        def __init__(self, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def get(self, url: str) -> _Response:
            await _fetch_json(url)
            return _Response(url)

    def _from_dict(body: dict[str, str]) -> _Set:
        if "a.example" in body["url"]:
            return _Set([_Key(end_user_issuer.KID, _PUBLIC), _Key("leaked", other_public)])
        return _Set([_Key("b-1", _PUBLIC)])

    monkeypatch.setattr(end_user_keys.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(end_user_keys.jwt.PyJWKSet, "from_dict", staticmethod(_from_dict))
    end_user_keys.clear()
    try:
        asyncio.run(end_user_keys.refresh())
        assert peak == 2, "the issuers are fetched side by side"
        assert end_user_keys.key(end_user_issuer.ISSUER, end_user_issuer.KID) is not None
        assert end_user_keys.key(end_user_issuer.ISSUER, "leaked") is None
        assert end_user_keys.loaded("second")
    finally:
        end_user_keys.clear()


def test_an_issuer_that_never_loads_leaves_the_others_on_their_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead `jwks_url` is retried every 2 s, and every issuer still gets its
    full fetch each period, so one that rotates keeps working."""
    import json

    from server.auth import end_user_keys
    from server.settings import settings

    issuers = json.loads(
        end_user_issuer.issuers_setting(_PUBLIC, keys={}, jwks_url="https://a.example/jwks")
    )
    issuers["dead"] = {"orgs": ["zeta"], "jwks_url": "https://dead.example/jwks"}
    monkeypatch.setattr(settings, "jwt_end_user_issuers_json", json.dumps(issuers))
    monkeypatch.setattr(settings, "jwt_end_user_jwks_refresh_seconds", 10.0)
    clock = [0.0]
    asked: list[dict[str, Any]] = []

    class _Done(Exception):
        pass

    async def _sleep(seconds: float) -> None:
        clock[0] += seconds

    async def _refresh(**kw: Any) -> None:
        asked.append(kw)
        end_user_keys._keys[end_user_issuer.ISSUER] = {}  # the live one is loaded
        if len(asked) == 6:
            raise _Done

    monkeypatch.setattr(end_user_keys, "_now", lambda: clock[0])
    monkeypatch.setattr(end_user_keys.asyncio, "sleep", _sleep)
    monkeypatch.setattr(end_user_keys, "refresh", _refresh)
    end_user_keys.clear()
    end_user_keys._keys[end_user_issuer.ISSUER] = {}

    async def _run() -> None:
        with pytest.raises(_Done):
            await end_user_keys.start_refresh_loop()

    try:
        asyncio.run(_run())
    finally:
        end_user_keys.clear()
    fast = {"unloaded_only": True}
    assert asked == [fast, fast, fast, fast, {}, fast], "a full fetch every 10 s"


def test_an_end_user_s_subject_revocation_names_its_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """`user:<id>` names one person per org: revoking acme's user:u-1 leaves
    globex's user:u-1 working, and the bare id revokes neither."""
    from server.auth import revocation

    def _revoke(*keys: str) -> None:
        monkeypatch.setattr(revocation, "_revoked_jtis", frozenset(keys))

    _revoke("user:u-1")
    assert verify_token(_token(org="acme")).org == "acme"
    _revoke("acme/user:u-1")
    with pytest.raises(TokenError, match="revoked"):
        verify_token(_token(org="acme"))
    assert verify_token(_token(org="globex")).org == "globex"


def test_a_token_with_ns_write_creates_no_collection_outside_it() -> None:
    from server.auth import context
    from server.core.errors import AuthInsufficientError
    from server.identity.api.models_collections import CollectionCreateInput
    from server.identity.app.collections import handle_collection_create

    context.set_current_auth(
        _person(namespaces=("*",), roles=(Role.WRITER,), write_namespaces=("ns-b",))
    )
    try:
        with pytest.raises(AuthInsufficientError, match="may not write"):
            asyncio.run(handle_collection_create(CollectionCreateInput(org=ORG, namespace="col-x")))
    finally:
        context.set_current_auth(None)


@pytest.mark.parametrize("call", ["move", "retire"])
def test_a_tree_write_whose_reply_is_lost_still_moves_the_watermark(
    monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """A detach or a retire narrows access: the row may have landed, and the
    retry finds nothing to do, so the failed call moves the watermark."""
    from contextlib import asynccontextmanager

    from server.identity.api.models_collections import CollectionMoveInput
    from server.identity.app import collections

    row = {
        "namespace": "col-q3-docs",
        "parent": "col-q3",
        "owner": "user:alice",
        "created_at": 0,
        "version": 1,
    }
    moved: list[str] = []

    @asynccontextmanager
    async def _claim(*_a: Any) -> Any:
        yield

    async def _fetch(org: str, ns: str) -> dict[str, Any]:
        return dict(row)

    async def _children(org: str, ns: str) -> list[dict[str, Any]]:
        return []

    async def _lost(*_a: Any, **_k: Any) -> None:
        raise TimeoutError("reply lost")

    async def _changed(org: str, *_a: Any, kind: str = "", **_k: Any) -> None:
        moved.append(kind)

    monkeypatch.setattr(collections, "collections_change", _claim)
    monkeypatch.setattr(collections, "resolve_subject", lambda: "user:alice")
    monkeypatch.setattr(collections.collections_repo, "fetch", _fetch)
    monkeypatch.setattr(collections.collections_repo, "children", _children)
    monkeypatch.setattr(collections.collections_repo, "write", _lost)
    monkeypatch.setattr(collections.grant_cache, "changed", _changed)
    with pytest.raises(TimeoutError):
        if call == "move":
            detach = CollectionMoveInput(org=ORG, namespace="col-q3-docs", parent="")
            asyncio.run(collections.handle_collection_move(detach))
        else:
            asyncio.run(collections.retire(ORG, "col-q3-docs"))
    assert moved == [grant_cache.COLLECTIONS]


def test_a_collection_description_is_held_to_2000_characters() -> None:
    from pydantic import ValidationError

    from server.identity.api.models_collections import CollectionCreateInput

    CollectionCreateInput(org="o", namespace="col-a", description="x" * 2000)
    with pytest.raises(ValidationError):
        CollectionCreateInput(org="o", namespace="col-a", description="x" * 2001)
