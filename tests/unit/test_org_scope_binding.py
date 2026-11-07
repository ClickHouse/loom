"""An org admin's see-everything filter covers its own org only.

`resolve_org_scope` is the privacy filter of every org-wide rollup. The op
gate already refuses a cross-org call, but the filter must not depend on
it: an org_admin of one org naming another gets that org's member filter,
and only a platform admin spans orgs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from server.auth import context
from server.auth.jwt import AuthInfo
from server.operations.base import Role


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def hidden(org: str) -> list[str]:
        return []

    async def mine(org: str, subject: str | None = None) -> list[str]:
        return ["own-ns"] if org == "a" else []

    monkeypatch.setattr(context, "restricted_namespaces_hidden_from", hidden)
    monkeypatch.setattr(context, "resolve_my_namespaces", mine)
    yield
    context.set_current_auth(None)


def _scope(org_of_token: str, role: Role, org: str, namespaces: tuple[str, ...] = ("*",)) -> Any:
    context.set_current_auth(
        AuthInfo(
            subject="op",
            org=org_of_token,
            namespaces=namespaces,
            roles=(role,),
            is_service_account=False,
        )
    )
    return asyncio.run(context.resolve_org_scope(org))


# An org admin's view leaves out the collections its grants do not reach.
_UNREAD_COLLECTIONS = (
    " AND (NOT startsWith(namespace, {col_prefix:String})"
    " OR namespace IN {col_readable:Array(String)})"
)


def test_an_org_admin_sees_its_own_org_but_the_collections_it_cannot_read() -> None:
    out = _scope("a", Role.ORG_ADMIN, "a")
    assert out.where == _UNREAD_COLLECTIONS
    assert out.params == {"col_prefix": "col-", "col_readable": []}


def test_an_org_admin_of_another_org_gets_the_member_filter() -> None:
    out = _scope("a", Role.ORG_ADMIN, "b")
    assert out.accessible is False  # no namespace of org b is theirs


def test_a_platform_admin_spans_orgs_whole() -> None:
    assert _scope("a", Role.PLATFORM_ADMIN, "b").where == ""


def test_a_wildcard_org_admin_spans_orgs_but_their_unread_collections() -> None:
    assert _scope("*", Role.ORG_ADMIN, "b").where == _UNREAD_COLLECTIONS


def test_an_org_admin_whose_claim_lists_namespaces_reads_as_a_member() -> None:
    """The gate admits such a token to no other namespace by name, so the org-wide
    rollup does not reach one either."""
    out = _scope("a", Role.ORG_ADMIN, "a", namespaces=("own-ns",))
    assert out.where == " AND namespace IN {ns_list:Array(String)}"
    assert out.params == {"ns_list": ["own-ns"]}


def test_a_platform_admin_whose_session_lists_its_namespaces_reads_the_org_whole() -> None:
    """A platform admin's dashboard session names only the person's own namespaces."""
    out = _scope("a", Role.PLATFORM_ADMIN, "b", namespaces=("own-ns",))
    assert out.accessible and "ns_list" not in out.params  # no member filter
