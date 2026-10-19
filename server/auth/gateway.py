"""A gateway's token acts for the end users of the service in front of Loom.

The service holds one token per org (`loom mint-token --gateway`) and names, on
every request, the user it acts for (`X-Loom-On-Behalf-Of: user:<id>`) and, when
the request runs inside one of its agents, that agent (`X-Loom-Agent: agent:<id>`).
The request then runs as that user alone: no namespace or role of the token
carries over (the user is an org member with no claim), its reads are pinned to
that user's rows (`own_user`), and what it reaches is what grants give
`user:<id>`, the user's groups and the agent.

The gateway is trusted to authenticate its users, as it already holds their
data. Loom bounds what it can claim: one org, a `user:` or `agent:` id, nothing
else (no group, role, namespace or Loom account). Ids are lowercase
(server/core/principals.py).
"""

from __future__ import annotations

import dataclasses

from server.auth.jwt import AuthInfo
from server.core.principals import AGENT, GROUP, PRINCIPAL_PREFIXES, USER, principal_id
from server.operations.base import Role

__all__ = [
    "AGENT",
    "GROUP",
    "PRINCIPAL_PREFIXES",
    "USER",
    "act_for",
    "effective_auth",
    "is_end_user",
]


def is_end_user(auth: AuthInfo | None) -> bool:
    """True when the request runs as a user a gateway asserted."""
    return auth is not None and bool(principal_id(auth.subject, USER))


def act_for(gateway: AuthInfo, on_behalf_of: str, agent: str = "") -> AuthInfo:
    """The identity a gateway request runs as. PermissionError when the
    headers do not name a user, or name anything but a user and an agent."""
    user = principal_id(on_behalf_of.strip(), USER)
    if not user:
        raise PermissionError(
            "a gateway token acts for one end user per request: send X-Loom-On-Behalf-Of: user:<id>"
        )
    agent = agent.strip()
    if agent and not principal_id(agent, AGENT):
        raise PermissionError("X-Loom-Agent names an agent: agent:<id>")
    return dataclasses.replace(
        gateway,
        subject=USER + user,
        namespaces=(),
        # A member of the org with no claim: an org-level read it may make, a
        # namespace only through a grant.
        roles=(Role.VIEWER,),
        write_namespaces=(),
        ns_roles=(),
        is_service_account=False,
        impersonated_by=gateway.subject,
        read_scope="own_user",
        read_user_id=user,
        gateway=False,
        agent_principal=agent,
    )


def effective_auth(auth: AuthInfo, on_behalf_of: str, agent: str = "") -> AuthInfo:
    """The identity an HTTP request with these headers runs as: the end user a
    gateway token names, the Loom subject a trusted caller acts for, or the
    bearer's own. PermissionError for a header the bearer may not send."""
    from server.settings import settings

    if auth.gateway:
        return act_for(auth, on_behalf_of, agent)
    trusted = {s.strip() for s in settings.trusted_callers.split(",") if s.strip()}
    on_behalf_of = on_behalf_of.strip()
    if not on_behalf_of or auth.subject not in trusted:
        return auth
    # A trusted caller acts for a Loom subject; only a gateway token becomes a
    # grant principal.
    if on_behalf_of.startswith(PRINCIPAL_PREFIXES):
        raise PermissionError("a trusted caller acts for a Loom subject, not a grant principal")
    return dataclasses.replace(auth, subject=on_behalf_of, impersonated_by=auth.subject)
