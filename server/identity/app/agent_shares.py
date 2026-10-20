"""grants.{share_agent,unshare_agent}: an agent lends its own namespace to
another agent of its trust, or takes it back.

The caller is the agent itself (a token `sa:<trust>:<agent>` from a workload
exchange, `server/auth/agents.py`), and it reaches only its own namespace and
the subjects of its own trust's agents, so a share never widens what the
caller could already read. Its control plane decides which agent may read
which, and calls these as the lending agent. The grantee's next token names
the namespace (`agents.with_grants`); a revoke caps the claim at once.
"""

from __future__ import annotations

from server.auth import agents
from server.core.errors import AuthInsufficientError, ValidationError
from server.identity.api.models_grants import (
    GrantsCreateInput,
    GrantsRevokeInput,
    GrantsShareAgentInput,
    GrantsShareAgentOutput,
    GrantsUnshareAgentInput,
    GrantsUnshareAgentOutput,
)
from server.identity.app.grants import handle_grants_create, handle_grants_revoke


def _grantee(org: str, namespace: str, agent: str) -> str:
    """The subject of `agent` in the caller's trust; refused unless the caller
    is an agent acting on its own namespace."""
    from server.auth.context import current_auth

    auth = current_auth()
    found = agents.parse(auth.subject) if auth is not None else None
    if auth is None or found is None or auth.org != org:
        raise AuthInsufficientError("only an agent shares its namespace, with grants.share_agent")
    trust, me = found
    if namespace != agents.namespace(trust, me):
        raise AuthInsufficientError(f"an agent shares only its own namespace, {trust}-{me}")
    if not agents.AGENT.fullmatch(agent):
        raise ValidationError("agent: lower-case letters, digits and '-', at most 63")
    if agent == me:
        raise ValidationError("an agent already owns its namespace")
    return agents.subject(trust, agent)


async def handle_grants_share_agent(input: GrantsShareAgentInput) -> GrantsShareAgentOutput:
    """Grant the agent `input.role` on the caller's namespace. A grant it holds
    already at a higher role (an admin's) is kept: 409 GRANT_WOULD_DOWNGRADE."""
    subject = _grantee(input.org, input.namespace, input.agent)
    out = await handle_grants_create(
        GrantsCreateInput(
            org=input.org, namespace=input.namespace, subject=subject, role=input.role
        )
    )
    return GrantsShareAgentOutput(
        namespace=input.namespace, subject=subject, role=out.role, granted_at=out.granted_at
    )


async def handle_grants_unshare_agent(input: GrantsUnshareAgentInput) -> GrantsUnshareAgentOutput:
    """Revoke the agent's grant on the caller's namespace; idempotent."""
    subject = _grantee(input.org, input.namespace, input.agent)
    out = await handle_grants_revoke(
        GrantsRevokeInput(org=input.org, namespace=input.namespace, subject=subject)
    )
    return GrantsUnshareAgentOutput(revoked=out.revoked, revoked_at=out.revoked_at)
