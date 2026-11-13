"""The places a knowledge read names besides its own namespace, each checked as a read."""

from __future__ import annotations


async def named_namespaces(org: str, namespace: str, names: list[str]) -> list[str]:
    """`namespace` (the transport gated it) and `names`, each gated as a read of
    its own (`@me` is the end user's personal collection): AuthInsufficientError
    for the first the caller may not read."""
    from server.auth.authz import check_authorized
    from server.auth.context import current_auth
    from server.auth.gateway import personal_namespace
    from server.core.errors import AuthInsufficientError, ValidationError
    from server.operations.base import Role

    auth = current_auth()
    out = [namespace]
    for name in names:
        try:
            place = personal_namespace(auth, org, name)
        except ValueError as e:
            raise ValidationError(str(e)) from e
        if place in out:
            continue
        try:
            await check_authorized(auth, Role.VIEWER, org=org, namespace=place)
        except PermissionError as e:
            raise AuthInsufficientError(str(e)) from e
        out.append(place)
    return out
