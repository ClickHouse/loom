"""grants.share_agent / unshare_agent: an agent lends only its own namespace,
only to agents of its own trust, and nobody else calls them."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError, ValidationError
from server.identity.api.models_grants import (
    GrantsCreateOutput,
    GrantsRevokeOutput,
    GrantsShareAgentInput,
    GrantsUnshareAgentInput,
)
from server.identity.app import agent_shares
from server.operations.base import Role

_NOW = datetime(2026, 10, 20, 12, 0, 0)


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []

    async def create(input: Any) -> GrantsCreateOutput:
        seen.append(("create", input))
        return GrantsCreateOutput(
            org=input.org,
            namespace=input.namespace,
            subject=input.subject,
            role=input.role,
            granted_at=_NOW,
        )

    async def revoke(input: Any) -> GrantsRevokeOutput:
        seen.append(("revoke", input))
        return GrantsRevokeOutput(revoked=True, revoked_at=_NOW)

    monkeypatch.setattr(agent_shares, "handle_grants_create", create)
    monkeypatch.setattr(agent_shares, "handle_grants_revoke", revoke)
    yield seen
    set_current_auth(None)


def _as(subject: str, org: str = "acme") -> None:
    set_current_auth(
        AuthInfo(
            subject=subject,
            org=org,
            namespaces=("fleet-a",),
            roles=(),
            is_service_account=True,
            ns_roles=(("fleet-a", "writer"),),
        )
    )


def _share(**over: Any) -> Any:
    body = {"org": "acme", "namespace": "fleet-a", "agent": "b"} | over
    return asyncio.run(agent_shares.handle_grants_share_agent(GrantsShareAgentInput(**body)))


def test_an_agent_lends_its_namespace_to_an_agent_of_its_trust(calls) -> None:
    _as("sa:fleet:a")
    out = _share()
    [(op, grant)] = calls
    assert op == "create" and grant.subject == "sa:fleet:b" and grant.role == "viewer"
    assert (grant.namespace, grant.replace) == ("fleet-a", False)  # an admin's grant is kept
    assert out.subject == "sa:fleet:b" and out.role == "viewer" and out.granted_at == _NOW
    assert _share(role="writer").role == "writer"


def test_an_agent_takes_its_share_back(calls) -> None:
    _as("sa:fleet:a")
    body = GrantsUnshareAgentInput(org="acme", namespace="fleet-a", agent="b")
    out = asyncio.run(agent_shares.handle_grants_unshare_agent(body))
    [(op, revoke)] = calls
    assert op == "revoke" and revoke.subject == "sa:fleet:b" and revoke.namespace == "fleet-a"
    assert out.revoked and out.revoked_at == _NOW


@pytest.mark.parametrize(
    "subject, org, namespace",
    [
        ("sa:fleet:a", "acme", "fleet-b"),  # another agent's namespace
        ("sa:fleet:a", "acme", "docs"),  # a namespace the trust shares
        ("sa:other:a", "acme", "fleet-a"),  # an agent of another trust
        ("sa:fleet:a", "beta", "fleet-a"),  # its namespace in another org
        ("sa:fleet", "acme", "fleet-a"),  # the trust itself
        ("alice@acme.com", "acme", "fleet-a"),  # a person, even an admin there
    ],
)
def test_nobody_else_shares_and_no_other_namespace_is_shared(
    calls, subject: str, org: str, namespace: str
) -> None:
    _as(subject, org)
    with pytest.raises(AuthInsufficientError):
        _share(namespace=namespace)
    assert calls == []


def test_with_auth_off_there_is_no_agent_to_share(calls) -> None:
    set_current_auth(None)
    with pytest.raises(AuthInsufficientError):
        _share()
    assert calls == []


@pytest.mark.parametrize("agent", ["a", "B", "b:c", "-b", "b" * 64, ""])
def test_the_grantee_is_another_agent_named_as_loom_takes_it(calls, agent: str) -> None:
    _as("sa:fleet:a")
    with pytest.raises(ValidationError):
        _share(agent=agent)
    assert calls == []


def test_a_share_never_grants_admin() -> None:
    with pytest.raises(ValueError):
        GrantsShareAgentInput(org="acme", namespace="fleet-a", agent="b", role="admin")


def test_the_ops_need_only_the_agents_writer_role_on_its_namespace() -> None:
    from server.identity.api import ops_grants

    for op in (ops_grants.grants_share_agent, ops_grants.grants_unshare_agent):
        assert op.required_role is Role.WRITER and op.audit
