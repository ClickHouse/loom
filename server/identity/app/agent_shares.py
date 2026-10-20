"""grants.{share_agent,unshare_agent}: an agent lends its own namespace to
another agent of its trust, or takes it back.

The caller is the agent itself (a token `sa:<trust>:<agent>` from a workload
exchange, `server/auth/agents.py`), and it reaches only its own namespace and
the subjects of its own trust's agents, so a share never widens what the
caller could already read. Its control plane decides which agent may read
which, and calls these as the lending agent. The grantee's next token names
the namespace (`agents.with_grants`); a revoke caps the claim at once.

An agent takes back only what it lent: a grant an admin made stays, as does
any grant on a restricted namespace, which only its admin gives.
"""

from __future__ import annotations

from server.auth import agents
from server.core.errors import AuthInsufficientError, PreconditionFailedError, ValidationError
from server.identity.api.models_grants import (
    GrantsCreateInput,
    GrantsRevokeInput,
    GrantsShareAgentInput,
    GrantsShareAgentOutput,
    GrantsUnshareAgentInput,
    GrantsUnshareAgentOutput,
)
from server.identity.app.grants import handle_grants_create, revoke_grant
from server.identity.domain.grants import GrantState


def _parties(org: str, namespace: str, agent: str) -> tuple[str, str]:
    """(the caller's subject, the subject of `agent` in its trust); refused
    unless the caller is an agent acting on its own namespace."""
    from server.auth.context import current_auth

    auth = current_auth()
    found = agents.parse(auth.subject) if auth is not None else None
    if auth is None or found is None or auth.org != org:
        raise AuthInsufficientError("only an agent shares its namespace, with grants.share_agent")
    trust, me = found
    own = agents.namespace(trust, me)
    if namespace != own:
        raise AuthInsufficientError(f"an agent shares only its own namespace, {own}")
    if not agents.AGENT.fullmatch(agent):
        raise ValidationError("agent: lower-case letters, digits and '-', at most 63")
    if agent == me:
        raise ValidationError("an agent already owns its namespace")
    return auth.subject, agents.subject(trust, agent)


async def handle_grants_share_agent(input: GrantsShareAgentInput) -> GrantsShareAgentOutput:
    """Let another agent of the caller's trust read, or write, the caller's own namespace."""
    from server.auth.authz import may_be_restricted
    from server.core.grant_cache import grant_map

    _, subject = _parties(input.org, input.namespace, input.agent)
    if await may_be_restricted(input.org, input.namespace):
        # A restricted read needs a grant its admin gave, never a writer's.
        raise AuthInsufficientError(
            f"{input.namespace} is restricted: only its admin grants it, with grants.create"
        )
    held = (await grant_map(input.org, subject)).ranks
    lent = {ns for ns, rank in held.items() if rank > 0 and ns != agents.home_of(subject)}
    if input.namespace not in lent and len(lent) >= agents.MAX_LENT:
        raise PreconditionFailedError(
            f"agent {input.agent} holds {agents.MAX_LENT} namespaces already; "
            "a namespace must be taken back first"
        )
    # Through grants.create, never lowering: a higher grant is kept (409
    # GRANT_WOULD_DOWNGRADE), so an agent lowers its own share by taking it
    # back first, and never an admin's.
    out = await handle_grants_create(
        GrantsCreateInput(
            org=input.org, namespace=input.namespace, subject=subject, role=input.role
        )
    )
    return GrantsShareAgentOutput(
        namespace=input.namespace, subject=subject, role=input.role, granted_at=out.granted_at
    )


async def handle_grants_unshare_agent(input: GrantsUnshareAgentInput) -> GrantsUnshareAgentOutput:
    """Take back what the caller lent another agent of its trust."""
    me, subject = _parties(input.org, input.namespace, input.agent)

    def lent_by_me(row: GrantState) -> None:
        if row.granted_by != me or row.role == "admin":
            raise PreconditionFailedError(
                f"{subject}'s grant on {input.namespace} was made by {row.granted_by}, "
                "who revokes it with grants.revoke"
            )

    out = await revoke_grant(
        GrantsRevokeInput(org=input.org, namespace=input.namespace, subject=subject),
        lent_by_me,
    )
    return GrantsUnshareAgentOutput(revoked=out.revoked, revoked_at=out.revoked_at)
