"""`code.repos` admits a scoped (ns_roles) token.

A service account minted with per-namespace roles has no single namespace to
bind an org-level listing to; a role gate answering IDENTITY_INCOMPLETE (400)
would leave the client unable to learn which repositories it may read. The op
is deployment-scoped: the gate uses the
token's best per-namespace role, and the handler authorizes every entry."""

from __future__ import annotations

import pytest

from server.auth.authz import check_authorized
from server.auth.jwt import AuthInfo
from server.core.errors import IdentityIncompleteError
from server.operations.base import Role
from server.operations.registry import ALL_OPERATIONS


def _op(name: str):  # type: ignore[no-untyped-def]
    return next(op for op in ALL_OPERATIONS if op.name == name)


def _scoped() -> AuthInfo:
    return AuthInfo(
        subject="clickgap-private",
        org="clickhouse",
        namespaces=["clickgap-private", "code-example-private"],
        roles=(),
        is_service_account=True,
        ns_roles=(("clickgap-private", "admin"), ("code-example-private", "viewer")),
    )


def test_code_repos_is_deployment_scoped_and_still_skips_namespace_authz() -> None:
    op = _op("code.repos")
    assert op.deployment_scoped is True
    assert op.skip_namespace_authz is True and op.required_role == Role.VIEWER


async def test_scoped_token_passes_the_transport_gate_for_code_repos() -> None:
    op = _op("code.repos")
    # the transport's call shape for a skip_namespace_authz op: org from the body, no namespace
    await check_authorized(
        _scoped(),
        op.required_role,
        org="clickhouse",
        namespace=None,
        deployment_scoped=op.deployment_scoped,
    )
    # without the flag the same token is refused as incomplete
    with pytest.raises(IdentityIncompleteError):
        await check_authorized(
            _scoped(), op.required_role, org="clickhouse", namespace=None, deployment_scoped=False
        )
