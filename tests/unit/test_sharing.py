"""Knowledge sharing (docs/architecture/sharing.md): a gateway's end users, the
principals of a request, collections inherited down a tree, and the rows the
three new tables hold."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import gateway, sharing
from server.auth.jwt import AuthInfo, TokenError, mint_token, verify_token
from server.auth.scim_groups import COLUMNS as GROUP_COLUMNS
from server.auth.scim_groups import group_row
from server.core import grant_cache
from server.core.grant_cache import GrantMap, watermark_row
from server.identity.app.collections import check_placement
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
    with pytest.raises(TokenError):
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
    tree = sharing.Tree(parent={"a": "", "me": ""}, personal=frozenset({"me"}))
    with pytest.raises(Exception, match="not a collection"):
        check_placement(tree, "a", "elsewhere")
    with pytest.raises(Exception, match="personal"):
        check_placement(tree, "a", "me")


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


def _role(auth: AuthInfo, ns: str) -> Role | None:
    return asyncio.run(sharing.role(auth, ORG, ns))


def test_a_group_grant_on_a_parent_reaches_its_child(org_state: dict[str, Any]) -> None:
    org_state["grants"] = {"group:finance": {"col-q3": 1}}
    assert _role(_user("alice"), "col-q3-docs") is Role.VIEWER
    assert _role(_user("bob"), "col-q3-docs") is None
    assert asyncio.run(sharing.readable(_user("alice"), ORG)) == ["col-q3", "col-q3-docs"]


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
    assert asyncio.run(sharing.readable(_user("alice"), ORG)) == []


def test_another_org_s_token_reaches_no_collection(org_state: dict[str, Any]) -> None:
    org_state["grants"] = {"user:alice": {"col-q3": 3}}
    other = verify_token(
        mint_token("sa:chat", "globex", [], [], gateway=True, secret=SECRET), secret=SECRET
    )
    alice_elsewhere = gateway.act_for(other, "user:alice")
    with pytest.raises(PermissionError):
        asyncio.run(sharing.check(alice_elsewhere, Role.VIEWER, ORG, "col-q3"))
    assert asyncio.run(sharing.readable(alice_elsewhere, ORG)) == []


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

    many = fanout.Fanout(org=ORG, namespaces=tuple(f"col-{i}" for i in range(50)), cap=32)
    monkeypatch.setattr(settings, "knowledge_search_max_namespaces", 40)
    read, skipped = many.split("col-0", settings.knowledge_search_max_namespaces)
    assert len(read) == 41 and len(skipped) == 9
    assert len(many.split("col-0").read) == 33, "memory search keeps the fan-out's cap"
