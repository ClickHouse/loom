"""A process with no namespace directory wired cannot tell a restricted
namespace from any other, so every gate that reads the directory denies:
the same answer an unreadable directory gets."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import server.auth.authz as authz
import server.auth.context as ctx
from server.auth import directory
from server.auth.jwt import AuthInfo
from server.identity.app.directory import IdentityDirectory
from server.operations.base import Role

ORG = "acme"
NS = "team"


@pytest.fixture
def unwired() -> Iterator[None]:
    directory.use_directory(None)
    try:
        yield
    finally:
        directory.use_directory(IdentityDirectory())


def _auth() -> AuthInfo:
    return AuthInfo(
        subject="alice@x", org=ORG, namespaces=(NS,), roles=(Role.ADMIN,), is_service_account=False
    )


def test_an_unwired_lookup_raises_the_unavailable_class(unwired: None) -> None:
    with pytest.raises(directory.ClassificationUnavailable):
        directory.directory()


@pytest.mark.asyncio
async def test_the_authorization_gate_refuses(
    unwired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _grant(org: str, ns: str, subject: str) -> Role:
        return Role.ADMIN

    monkeypatch.setattr(authz, "grant_role_for", _grant)
    with pytest.raises(PermissionError, match="cannot access"):
        await authz.check_authorized(_auth(), Role.VIEWER, org=ORG, namespace=NS)


@pytest.mark.asyncio
async def test_the_namespace_lists_drop_everything(unwired: None) -> None:
    token = ctx.current_auth_var.set(_auth())
    try:
        assert await ctx._gate_readable(_auth(), ORG, [NS, "other"]) == []
        assert await ctx.hidden_namespaces(ORG) == ["*"]
    finally:
        ctx.current_auth_var.reset(token)
