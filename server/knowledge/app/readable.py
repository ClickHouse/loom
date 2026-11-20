"""The namespaces a knowledge read covers: its own and the ones it names, each checked as a read."""

from __future__ import annotations

from server.auth import authz
from server.auth.context import current_auth
from server.auth.gateway import personal_namespace
from server.core.errors import AuthInsufficientError, ValidationError
from server.operations.base import Role


async def readable_namespaces(org: str, namespace: str, names: list[str]) -> list[str]:
    """`namespace` (the transport gated it) and `names`, each gated as a read of
    its own (`@me` is the end user's personal collection): AuthInsufficientError
    for the first the caller may not read."""
    auth = current_auth()
    out = [namespace]
    for name in names:
        try:
            resolved = personal_namespace(auth, org, name)
        except ValueError as e:
            raise ValidationError(str(e)) from e
        if resolved in out:
            continue
        try:
            await authz.check_authorized(auth, Role.VIEWER, org=org, namespace=resolved)
        except PermissionError as e:
            raise AuthInsufficientError(str(e)) from e
        out.append(resolved)
    return out
