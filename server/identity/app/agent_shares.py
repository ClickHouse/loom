"""grants.{share_agent,unshare_agent}: an agent lends its own namespace to
another agent of its trust, or takes it back.

The caller is the agent itself (a token `sa:<trust>:<agent>` from a workload
exchange, `server/auth/agents.py`), and it reaches only its own namespace and
the subjects of its own trust's agents, so a share never widens what the
caller could already read. Its control plane decides which agent may read
which, and calls these as the lending agent. The grantee's next token names
the namespace (`agents.with_grants`); a revoke caps the claim at once.

An agent writes over and takes back only what it lent: a grant an admin made
stays. A restricted namespace is never shared this way.
"""

from __future__ import annotations

from collections.abc import Callable

from server.auth import agents, machines
from server.core.errors import (
    AuthInsufficientError,
    GrantWouldDowngradeError,
    PreconditionFailedError,
    ValidationError,
)
from server.identity.api.models_grants import (
    GrantsCreateInput,
    GrantsRevokeInput,
    GrantsShareAgentInput,
    GrantsShareAgentOutput,
    GrantsUnshareAgentInput,
    GrantsUnshareAgentOutput,
)
from server.identity.app.grants import create_grant, revoke_grant
from server.identity.domain.grants import GrantState


def _parties(org: str, namespace: str, agent: str) -> tuple[str, str, str]:
    """(the caller's trust, the caller's subject, the subject of `agent` in
    that trust); refused unless the caller is an agent acting on its own
    namespace."""
    from server.auth.context import current_auth

    auth = current_auth()
    found = agents.parse(auth.subject) if auth is not None else None
    if auth is None or found is None or auth.org != org:
        raise AuthInsufficientError(
            "only the agent whose namespace it is shares it or takes it back"
        )
    trust, me = found
    own = agents.namespace(trust, me)
    if namespace != own:
        raise AuthInsufficientError(f"an agent shares only its own namespace, {own}")
    if not agents.AGENT.fullmatch(agent):
        raise ValidationError("agent: lower-case letters, digits and '-', at most 63")
    if agent == me:
        raise ValidationError("an agent already owns its namespace")
    return trust, auth.subject, agents.subject(trust, agent)


def _made_by(me: str, subject: str, namespace: str) -> Callable[[GrantState], None]:
    """Refuses a grant someone else made, or an admin role: an agent writes
    over and takes back only its own share."""

    def check(row: GrantState) -> None:
        if row.granted_by != me or row.role == "admin":
            raise PreconditionFailedError(
                f"{subject}'s grant on {namespace} was made by {row.granted_by}, "
                "who changes it with grants.create or grants.revoke"
            )

    return check


async def handle_grants_share_agent(input: GrantsShareAgentInput) -> GrantsShareAgentOutput:
    """Let another agent of the caller's trust read, or write, the caller's own namespace."""
    from server.auth.authz import may_be_restricted

    trust, me, subject = _parties(input.org, input.namespace, input.agent)
    if await may_be_restricted(input.org, input.namespace):
        # A restricted read needs a grant its admin gave, never a writer's.
        raise AuthInsufficientError(
            f"{input.namespace} is restricted: only its admin grants it, with grants.create"
        )
    # Counted as the grantee's token counts them (`agents.with_grants`).
    m = await machines.get(trust)
    roles = {agents.home_of(subject): "writer", **dict(m["ns_roles"] if m else [])}
    extra = agents.lent(roles, await agents.granted(input.org, subject))
    if input.namespace not in extra and (
        agents.name_bytes([*extra, input.namespace]) > agents.LENT_NAME_BYTES
    ):
        raise PreconditionFailedError(
            f"agent {input.agent} holds as many lent namespaces as its token names; "
            "a namespace must be taken back first"
        )
    # Never lowering and never over someone else's grant: an agent lowers its
    # own share by taking it back first.
    try:
        out = await create_grant(
            GrantsCreateInput(
                org=input.org, namespace=input.namespace, subject=subject, role=input.role
            ),
            _made_by(me, subject, input.namespace),
        )
    except GrantWouldDowngradeError as e:
        current = str(e.extra.get("current_role", ""))
        raise GrantWouldDowngradeError(
            f"{subject} is {current} on {input.namespace}; a {input.role} share would lower "
            "it: take it back with grants.unshare_agent, then share again",
            current_role=current,
            requested_role=input.role,
        ) from None
    return GrantsShareAgentOutput(
        namespace=input.namespace, subject=subject, role=input.role, granted_at=out.granted_at
    )


async def handle_grants_unshare_agent(input: GrantsUnshareAgentInput) -> GrantsUnshareAgentOutput:
    """Take back what the caller lent another agent of its trust."""
    _, me, subject = _parties(input.org, input.namespace, input.agent)
    out = await revoke_grant(
        GrantsRevokeInput(org=input.org, namespace=input.namespace, subject=subject),
        _made_by(me, subject, input.namespace),
    )
    return GrantsUnshareAgentOutput(revoked=out.revoked, revoked_at=out.revoked_at)
