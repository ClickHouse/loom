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


def _scope(org_of_token: str, role: Role, org: str) -> Any:
    context.set_current_auth(
        AuthInfo(
            subject="op", org=org_of_token, namespaces=(), roles=(role,), is_service_account=False
        )
    )
    return asyncio.run(context.resolve_org_scope(org))


def test_an_org_admin_sees_its_own_org_whole() -> None:
    assert _scope("a", Role.ORG_ADMIN, "a").where == ""


def test_an_org_admin_of_another_org_gets_the_member_filter() -> None:
    out = _scope("a", Role.ORG_ADMIN, "b")
    assert out.accessible is False  # no namespace of org b is theirs


@pytest.mark.parametrize("token_org", ["a", "*"])
def test_a_platform_admin_or_wildcard_org_admin_spans_orgs(token_org: str) -> None:
    role = Role.PLATFORM_ADMIN if token_org == "a" else Role.ORG_ADMIN
    assert _scope(token_org, role, "b").where == ""
