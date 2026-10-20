"""Knowledge sharing (docs/architecture/sharing.md): a gateway's end users, the
principals of a request, collections inherited down a tree, and the rows the
three new tables hold."""

from __future__ import annotations

import asyncio
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
from tests.unit.schema_ddl import column_names

ORG = "acme"
SECRET = "s" * 32


def _gateway() -> AuthInfo:
    return verify_token(
        mint_token("sa:chat", ORG, [], [], gateway=True, secret=SECRET), secret=SECRET
    )


# ── the gateway token ─────────────────────────────────────────────────────


def test_a_gateway_token_carries_the_claim_and_nothing_else() -> None:
    auth = _gateway()
    assert auth.gateway and auth.namespaces == () and auth.roles == ()


@pytest.mark.parametrize(
    "kwargs",
    [{"namespaces": ["team"]}, {"roles": ["admin"]}, {"org": "*"}, {"is_service_account": False}],
)
def test_a_gateway_token_is_refused_with_any_claim(kwargs: dict[str, Any]) -> None:
    args: dict[str, Any] = {"subject": "sa:chat", "org": ORG, "namespaces": [], "roles": []}
    args.update(kwargs)
    with pytest.raises(TokenError):
        mint_token(**args, gateway=True, secret=SECRET)


@pytest.mark.parametrize("subject", ["user:alice", "group:finance", "agent:helper"])
def test_no_token_is_minted_for_a_grant_principal(subject: str) -> None:
    with pytest.raises(ValueError, match="grant principal"):
        mint_token(subject, ORG, ["team"], ["viewer"], secret=SECRET)


def test_a_request_runs_as_the_user_the_gateway_names_and_nothing_more() -> None:
    auth = gateway.act_for(_gateway(), "user:u-1", "agent:helper")
    assert auth.subject == "user:u-1"
    assert auth.org == ORG
    assert auth.namespaces == () and auth.ns_roles == () and auth.write_namespaces == ()
    assert auth.roles == (Role.VIEWER,)
    assert (auth.read_scope, auth.read_user_id) == ("own_user", "u-1")
    assert auth.impersonated_by == "sa:chat"
    assert auth.agent_principal == "agent:helper"
    assert not auth.gateway, "the asserted identity cannot assert again"


@pytest.mark.parametrize(
    ("on_behalf_of", "agent"),
    [
        ("", ""),
        ("alice@example.com", ""),  # a Loom account, not a gateway user
        ("group:finance", ""),
        ("sa:other", ""),
        ("user:", ""),
        ("user:a b", ""),
        ("user:u-1", "group:finance"),
        ("user:u-1", "u-2"),
        ("user:Alice", ""),  # ids are lowercase: two spellings never name one user
        ("user:u-1", "agent:Helper"),
    ],
)
def test_a_gateway_names_only_a_user_and_an_agent(on_behalf_of: str, agent: str) -> None:
    with pytest.raises(PermissionError):
        gateway.act_for(_gateway(), on_behalf_of, agent)


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
    return gateway.act_for(_gateway(), f"user:{name}", agent)


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


def test_an_agent_grant_counts_only_when_the_gateway_asserts_the_agent(
    org_state: dict[str, Any],
) -> None:
    org_state["grants"] = {"agent:helper": {"col-hr": 1}}
    assert _role(_user("bob", "agent:helper"), "col-hr") is Role.VIEWER
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
    other = verify_token(
        mint_token("sa:chat", "globex", [], [], gateway=True, secret=SECRET), secret=SECRET
    )
    alice_elsewhere = gateway.act_for(other, "user:alice")
    with pytest.raises(PermissionError):
        asyncio.run(sharing.check(alice_elsewhere, Role.VIEWER, ORG, "col-q3"))
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
    for header in ("user:u-1", "group:finance", "agent:helper"):
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
        with pytest.raises(PermissionError, match="not open to a gateway"):
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

    monkeypatch.setattr(authz, "_is_org_readable_code_namespace", _readable)
    monkeypatch.setattr(authz, "may_be_restricted", _not_restricted)
    member = _person(subject="ann@acme.io", is_service_account=False, roles=(Role.VIEWER,))
    asyncio.run(authz.check_authorized(member, Role.VIEWER, ORG, "code-acme"))
    with pytest.raises(PermissionError):
        asyncio.run(authz.check_authorized(_user("alice"), Role.VIEWER, ORG, "code-acme"))


@pytest.mark.parametrize("subject", ["Group:Eng", "AGENT:helper", "User:Bob"])
def test_a_grant_to_a_principal_in_another_case_is_refused(subject: str) -> None:
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
    from datetime import datetime

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
        async def query_one(self, sql: str, params: Any = None, **_k: Any) -> dict[str, Any]:
            assert all(c in sql for c in ("agent", "user_id", "actor", "source_class"))
            return stored

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


def test_a_gateway_token_acts_as_no_one_on_a_route_naming_no_end_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from server.auth import bearer

    monkeypatch.setattr(bearer, "verify_bearer", lambda _token: _gateway())
    with pytest.raises(HTTPException) as refused:
        bearer.verify_bearer_header("Bearer gw")
    assert refused.value.status_code == 403
    assert bearer.verify_bearer_header("Bearer gw", acts_for_end_users=True) is not None


def test_the_op_endpoint_runs_a_gateway_token_as_the_user_it_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.auth import bearer
    from server.transport import http

    monkeypatch.setattr(bearer, "verify_bearer", lambda _token: _gateway())
    headers = {"Authorization": "Bearer gw", "X-Loom-On-Behalf-Of": "user:alice"}
    auth = http._extract_auth(SimpleNamespace(headers=headers))  # type: ignore[arg-type]
    assert auth is not None and auth.subject == "user:alice"


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

    from server.identity.api.models import NamespaceDeleteInput
    from server.identity.app import collections, namespace

    directory = {"org": ORG, "namespace": "col-hr", "kind": "collection", "active": True}
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
    delete = NamespaceDeleteInput(org=ORG, namespace="col-hr")
    with pytest.raises(RuntimeError):
        asyncio.run(namespace.handle_namespace_delete(delete))
    assert directory["active"] is False and retired == ["failed"], "the tree still stands"
    asyncio.run(namespace.handle_namespace_delete(delete))
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


def test_mcp_refuses_a_gateway_token_as_not_allowed_not_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fastapi import Response

    from server.auth import jwt
    from server.transport import mcp_auth

    monkeypatch.setattr(jwt, "verify_token", lambda _token: _gateway())
    request = SimpleNamespace(headers={"authorization": "Bearer gw"})
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
    from datetime import datetime

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

        async def _emit(*_a: Any, **_k: Any) -> str:
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
    from datetime import datetime

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
    out = asyncio.run(
        namespace.handle_namespace_delete(NamespaceDeleteInput(org=ORG, namespace="col-hr"))
    )
    assert out.deleted is True and retired == ["col-hr"]


def test_an_end_users_original_never_passes_to_another_writer_of_new_text() -> None:
    """Without fresh bytes a write carries the prior original, but not an end
    user's to someone else: that user's erasure must still find it."""
    from datetime import datetime

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
