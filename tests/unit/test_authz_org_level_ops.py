"""An org-level op (no namespace in the body) still belongs to an org, and a
row fetched by an org-wide id is checked against its own namespace
. Found by the cross-principal leakage suite: memory.get_by_id
served any org's row to any token holding VIEWER."""

from __future__ import annotations

import asyncio

import pytest

import server.auth.authz as authz
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import IdentityIncompleteError
from server.operations.base import Role


def _flat(org: str, *roles: Role, namespaces=("ns-b",)) -> AuthInfo:
    return AuthInfo(
        subject="bob@x",
        org=org,
        namespaces=namespaces,
        roles=tuple(roles),
        is_service_account=False,
    )


def _scoped(org: str) -> AuthInfo:
    return AuthInfo(
        subject="sa:b",
        org=org,
        namespaces=("ns-b",),
        roles=(),
        is_service_account=True,
        ns_roles=(("ns-b", "writer"),),
    )


def test_other_org_is_refused_on_an_org_level_op() -> None:
    with pytest.raises(PermissionError):
        asyncio.run(
            authz.check_authorized(
                _flat("other", Role.VIEWER), required_role=Role.VIEWER, org="acme", namespace=None
            )
        )


def test_same_org_flat_role_still_passes_an_org_level_op() -> None:
    asyncio.run(
        authz.check_authorized(
            _flat("acme", Role.VIEWER), required_role=Role.VIEWER, org="acme", namespace=None
        )
    )


def test_wildcard_org_and_platform_admin_pass_an_org_level_op() -> None:
    asyncio.run(
        authz.check_authorized(
            _flat("*", Role.VIEWER), required_role=Role.VIEWER, org="acme", namespace=None
        )
    )
    asyncio.run(
        authz.check_authorized(
            _flat("other", Role.PLATFORM_ADMIN),
            required_role=Role.VIEWER,
            org="acme",
            namespace=None,
        )
    )


def test_row_in_scope_hides_a_foreign_namespace_and_a_foreign_org(monkeypatch) -> None:
    async def _no_grant(org, namespace, subject):
        return None

    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    set_current_auth(_flat("acme", Role.WRITER))
    assert asyncio.run(authz.row_in_scope("acme", "ns-b")) is True
    assert asyncio.run(authz.row_in_scope("acme", "ns-a")) is False
    assert asyncio.run(authz.row_in_scope("acme", "ns-b", Role.WRITER)) is True
    assert asyncio.run(authz.row_in_scope("acme", "ns-b", Role.ADMIN)) is False
    set_current_auth(_flat("other", Role.ADMIN))
    assert asyncio.run(authz.row_in_scope("acme", "ns-a", Role.VIEWER)) is False
    # A scoped token is its own ceiling: ns-b writer, nothing on ns-a.
    set_current_auth(_scoped("acme"))
    assert asyncio.run(authz.row_in_scope("acme", "ns-a")) is False
    assert asyncio.run(authz.row_in_scope("acme", "ns-b", Role.WRITER)) is True
    # No auth context (auth disabled) → open, as everywhere else.
    set_current_auth(None)
    assert asyncio.run(authz.row_in_scope("acme", "ns-a", Role.ADMIN)) is True


def test_scoped_token_on_an_org_level_op_is_identity_incomplete_not_a_leak() -> None:
    with pytest.raises((IdentityIncompleteError, PermissionError)):
        asyncio.run(
            authz.check_authorized(
                _scoped("acme"), required_role=Role.VIEWER, org="acme", namespace=None
            )
        )


def test_a_grant_in_another_org_does_not_admit_a_same_named_subject(monkeypatch) -> None:
    """Grants are keyed by subject name, and an org's admin can mint any subject:
    org other's token for "bob@x" must not ride acme's grant for "bob@x"."""
    seen: list[tuple[str, str, str]] = []

    async def _acme_grant(org, namespace, subject):
        seen.append((org, namespace, subject))
        return Role.ADMIN if org == "acme" else None

    async def _plain(org, namespace):
        return False

    monkeypatch.setattr(authz, "_lookup_grant", _acme_grant)
    monkeypatch.setattr(authz, "may_be_restricted", _plain)
    monkeypatch.setattr(authz, "is_org_readable_code_namespace", _plain)
    for role in (Role.VIEWER, Role.WRITER):
        with pytest.raises(PermissionError):
            asyncio.run(
                authz.check_authorized(
                    _flat("other", Role.ADMIN), required_role=role, org="acme", namespace="ns-a"
                )
            )
    assert seen == []  # refused before any grant is read
    set_current_auth(_flat("other", Role.ADMIN))
    assert asyncio.run(authz.row_in_scope("acme", "ns-a", Role.VIEWER)) is False
    set_current_auth(None)
    # The same subject in its own org still gets in through the grant.
    asyncio.run(
        authz.check_authorized(
            _flat("acme", Role.VIEWER), required_role=Role.WRITER, org="acme", namespace="ns-a"
        )
    )


def test_scoped_token_on_a_deployment_scoped_op_stays_in_its_own_org() -> None:
    """A deployment-scoped op (namespace.browse, grants.pending_for_me, …) is
    gated on the scoped token's best role, but the org it reads is still the
    token's own: `org` in the body names another org's directory and grants."""
    with pytest.raises(PermissionError):
        asyncio.run(
            authz.check_authorized(
                _scoped("other"),
                required_role=Role.VIEWER,
                org="acme",
                namespace=None,
                deployment_scoped=True,
            )
        )
    asyncio.run(
        authz.check_authorized(
            _scoped("acme"),
            required_role=Role.VIEWER,
            org="acme",
            namespace=None,
            deployment_scoped=True,
        )
    )
    asyncio.run(
        authz.check_authorized(
            _scoped("other"), required_role=Role.VIEWER, org=None, deployment_scoped=True
        )
    )
