"""The end users of a service in front of Loom (a chat app) and its agents.

The service signs a short-lived token per request for the user it serves, with
a key registered for it as an end-user issuer (`jwt_end_user_issuers_json`):
`sub: user:<id>`, one `org`, and, when the request runs inside one of its
agents, that agent (`agent: agent:<id>`). `server.auth.jwt` verifies it into
the user's identity: an org member with no claim, whose reads are pinned to
the user's rows (`own_user`), and who reaches what grants give `user:<id>`,
the user's groups, the agent and everyone in the org. The service decides who
may use which agent before it signs one in, so Loom keeps no copy of that.

An end-user key verifies only its own issuer's tokens, and only as an end
user. Ids are lowercase (server/core/principals.py).
"""

from __future__ import annotations

import dataclasses

from server.auth.jwt import AuthInfo
from server.core.principals import USER, is_reserved_subject, personal_name, principal_id

__all__ = ["effective_auth", "is_end_user", "personal_namespace"]


def is_end_user(auth: AuthInfo | None) -> bool:
    """True when the request runs as an end user an issuer signed in."""
    return auth is not None and bool(principal_id(auth.subject, USER))


def effective_auth(auth: AuthInfo, on_behalf_of: str) -> AuthInfo:
    """The identity an HTTP request runs as: the Loom subject a trusted caller
    names in `X-Loom-On-Behalf-Of`, else the bearer's own. PermissionError for
    a header the bearer may not send: a trusted caller acts for a Loom
    subject, never a grant principal (only an end-user issuer signs those)."""
    from server.settings import settings

    on_behalf_of = on_behalf_of.strip()
    if not on_behalf_of or auth.subject not in settings.trusted_callers_set:
        return auth
    if is_reserved_subject(on_behalf_of):
        raise PermissionError("a trusted caller acts for a Loom subject, not a grant principal")
    return dataclasses.replace(auth, subject=on_behalf_of, impersonated_by=auth.subject)


def personal_namespace(auth: AuthInfo | None, org: str, namespace: str) -> str:
    """`namespace`, with `@me` read as the end user's personal collection (no
    read: the name is derived from the user). ValueError for `@me` from any
    caller that is not an end user."""
    from server.auth.sharing import PERSONAL_ALIAS

    if namespace != PERSONAL_ALIAS:
        return namespace
    if auth is None or not is_end_user(auth):
        raise ValueError(f"{PERSONAL_ALIAS!r} names an end user's personal collection")
    return personal_name(org, auth.subject)
