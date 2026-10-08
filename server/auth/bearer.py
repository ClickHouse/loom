"""The bearer credential of a request, read and verified one way.

`bearer_token` reads an `Authorization` header for every route and both
transports: absent or blank is no credential, `Bearer <token>` (the scheme
is case-insensitive) is the token, and anything else is a credential Loom
cannot read, refused like a bad token.

The op endpoint (`server.transport.http`) and the routes outside the
registry (`/v1/me/*`, `/v1/admin/*`, the task-watch stream) answer a bad
token the same way: 401 AUTH_INVALID, and 503 AUTH_UNAVAILABLE with
Retry-After while the revocation list has not loaded yet — a 401 then would
tell the client to discard a good token.
"""

from __future__ import annotations

from fastapi import HTTPException

from server.auth.jwt import AuthInfo, RevocationUnavailable, TokenError, verify_token


def bearer_token(authorization: str | None) -> str | None:
    """The token an `Authorization` header carries, None when it carries no
    credential. Raises TokenError for a header that is neither blank nor
    `Bearer <token>` (another scheme, or the scheme with no token)."""
    text = (authorization or "").strip()
    if not text:
        return None
    scheme, _, token = text.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise TokenError("authorization header must be `Bearer <token>`")
    return token


def verify_bearer_header(authorization: str | None) -> AuthInfo | None:
    """The principal an `Authorization` header names, None when it carries
    no credential; HTTPException 401 / 503 as `verify_bearer`."""
    try:
        token = bearer_token(authorization)
    except TokenError as e:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_INVALID", "message": str(e)},
        ) from e
    return None if token is None else verify_bearer(token)


def verify_bearer(token: str) -> AuthInfo:
    """The principal `token` names; HTTPException 401 / 503 otherwise."""
    try:
        return verify_token(token)
    except RevocationUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"code": "AUTH_UNAVAILABLE", "message": str(e)},
            headers={"Retry-After": "5"},
        ) from e
    except TokenError as e:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_INVALID", "message": str(e)},
        ) from e
