"""Authenticated principals for unit tests.

    auth = principal("viewer", namespaces=("shop",))
    with acting_as(auth):
        await handler(inp)

`principal` fills the fields every `AuthInfo` needs with one tenant's
defaults, so a test states only what its scenario is about. `acting_as` puts
the principal in `current_auth_var` and always takes it back out, the rule
the unit conftest's auth guard enforces.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "acme"
NAMESPACE = "shop"


def principal(
    *roles: Role | str,
    org: str = ORG,
    namespaces: Sequence[str] = (NAMESPACE,),
    subject: str | None = None,
    service_account: bool = True,
    **fields: Any,
) -> AuthInfo:
    """An `AuthInfo` holding `roles` (names or `Role`s; none by default) on
    `namespaces` of `org`. Any other `AuthInfo` field passes through."""
    resolved = tuple(r if isinstance(r, Role) else Role[r.upper()] for r in roles)
    if subject is None:
        label = resolved[0].name.lower() if resolved else "caller"
        subject = f"sa:{label}" if service_account else f"{label}@example.com"
    return AuthInfo(
        subject=subject,
        org=org,
        namespaces=tuple(namespaces),
        roles=resolved,
        is_service_account=service_account,
        **fields,
    )


@contextmanager
def acting_as(auth: AuthInfo | None) -> Iterator[AuthInfo | None]:
    token = current_auth_var.set(auth)
    try:
        yield auth
    finally:
        current_auth_var.reset(token)
