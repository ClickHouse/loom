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
else (no group, role, namespace or Loom account).
"""

from __future__ import annotations

import dataclasses
import re

from server.auth.jwt import AuthInfo
from server.operations.base import Role

USER, GROUP, AGENT = "user:", "group:", "agent:"
PRINCIPAL_PREFIXES = (USER, GROUP, AGENT)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")


def principal_id(value: str, prefix: str) -> str:
    """The id of `value` when it is `prefix<id>` with a well-formed id, else ''."""
    if not value.startswith(prefix):
        return ""
    ident = value[len(prefix) :]
    return ident if _ID.fullmatch(ident) else ""


def is_principal(subject: str) -> bool:
    """True for a `user:`, `group:` or `agent:` grant subject with a well-formed id."""
    return any(principal_id(subject, p) for p in PRINCIPAL_PREFIXES)


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
