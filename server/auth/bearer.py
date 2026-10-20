"""The bearer credential of a request, read and verified one way.

`bearer_token` reads an `Authorization` header for every route and both
transports: absent or blank is no credential, `Bearer <token>` (the scheme
is case-insensitive) is the token, and anything else is a credential Loom
cannot read, refused like a bad token.

The op endpoint (`server.transport.http`) and the routes outside the
registry (`/v1/me/*`, `/v1/admin/*`, the task-watch stream) answer a bad
token the same way: 401 AUTH_INVALID, and 503 AUTH_UNAVAILABLE with
Retry-After while the revocation list has not loaded yet — a 401 then would
tell the client to discard a good token. Every refusal here is counted in
`loom.auth.refusals`: the op ledger has no row for it (a `/v1/me/*` route
writes its own `me.*` row) and the SLI rollup none at all.
"""

from __future__ import annotations

from fastapi import HTTPException

from server.auth.context import quota_key_for
from server.auth.jwt import AuthInfo, AuthUnavailable, TokenError, verify_token
from server.core.ch import set_quota_key
from server.observability.metrics import record_auth_refusal


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
        raise TokenError("authorization header must be `Bearer <token>`", reason="malformed")
    return token


def missing_bearer() -> HTTPException:
    """The 401 for a request that carries no bearer where one is required,
    counted in `loom.auth.refusals` as `missing`."""
    record_auth_refusal(None, transport="http")
    return HTTPException(
        status_code=401,
        detail={"code": "AUTH_INVALID", "message": "missing bearer token"},
    )


def verify_bearer_header(
    authorization: str | None, *, acts_for_end_users: bool = False
) -> AuthInfo | None:
    """The principal an `Authorization` header names, None when it carries
    no credential; HTTPException 401 / 503 as `verify_bearer`, and 403 for a
    gateway token unless the route applies the end user a request names
    (`acts_for_end_users`): a gateway token acts as no one itself."""
    try:
        token = bearer_token(authorization)
    except TokenError as e:
        record_auth_refusal(e, transport="http")
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_INVALID", "message": str(e)},
        ) from e
    info = None if token is None else verify_bearer(token)
    if info is not None and info.gateway and not acts_for_end_users:
        raise gateway_refused()
    return info


def gateway_refused() -> HTTPException:
    """The 403 for a gateway token on a route that names no end user."""
    return HTTPException(
        status_code=403,
        detail={
            "code": "AUTH_INSUFFICIENT",
            "message": "a gateway token acts only for the end user a request names",
        },
    )


def auth_unavailable(e: AuthUnavailable) -> HTTPException:
    """503 AUTH_UNAVAILABLE with Retry-After: the token may be good, try again."""
    return HTTPException(
        status_code=503,
        detail={"code": "AUTH_UNAVAILABLE", "message": str(e)},
        headers={"Retry-After": "5"},
    )


def verify_bearer(token: str) -> AuthInfo:
    """The principal `token` names; HTTPException 401 / 503 otherwise. The
    rest of the request's ClickHouse queries carry its quota key, so routes
    outside the op registry draw the caller's bucket too."""
    try:
        info = verify_token(token)
    except TokenError as e:
        record_auth_refusal(e, transport="http")
        if isinstance(e, AuthUnavailable):
            raise auth_unavailable(e) from e
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_INVALID", "message": str(e)},
        ) from e
    set_quota_key(quota_key_for(info))
    return info
