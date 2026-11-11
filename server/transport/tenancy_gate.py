"""A cell's tenancy check for the routes outside the op registry that act in the
verified token's own org, or in the sign-up org an org-less or `*` token is served in
(`acting_org`): that org is one this cell holds, placed or refused before the route runs
(docs/architecture/cells.md, "The tenancy check and placement").

The op routes and MCP check the org each call acts in at the op gate
(`gates.authorize`); a route that acts in an org its request names checks that org
itself (`check_authorized`, `admit_org`); the rest act in no org. Only the routes
`acts_in_token_org` names are checked here, the ones the route census marks `token`,
so a platform admin whose own org lives elsewhere still reaches the routes that act
in no org or in a held one. A request with no bearer, a malformed header or a token
that does not verify passes to the route, which answers it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from server.auth import tenancy
from server.core.errors import (
    LoomServerError,
    classified,
    error_help_link,
    shown_unauthenticated,
)
from server.settings import settings

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo

log = logging.getLogger(__name__)

# The routes that act in the caller's org (the route census's `token`): the token's, or
# on two of them the sign-up org (`acting_org`).
_TOKEN_ORG_ROUTES = frozenset({"/v1/assistant/chat/completions"})
_TOKEN_ORG_PREFIXES = ("/scim/v2/Groups", "/v1/me/")


def acts_in_token_org(path: str) -> bool:
    return path in _TOKEN_ORG_ROUTES or path.startswith(_TOKEN_ORG_PREFIXES)


# The caller's own routes that serve an org-wide (`*`) or org-less token in the sign-up
# org (`me_callers.caller_identity`); the others act in the token's org as it is.
_SIGN_UP_ORG_ROUTES = frozenset({"/v1/me/set-default-namespace", "/v1/me/sharing-summary"})


def acting_org(path: str, info: AuthInfo) -> str:
    """The org a `token` route acts in: the token's, or for an org-wide or org-less token
    on the routes that resolve one through `caller_identity`, the sign-up org."""
    if path in _SIGN_UP_ORG_ROUTES and info.org in ("", "*"):
        return settings.self_signup_org
    return info.org


class TenancyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not settings.cell or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "").rstrip("/") or "/"
        if not acts_in_token_org(path):
            await self.app(scope, receive, send)
            return
        from server.auth.jwt import AuthUnavailable, TokenError, verify_token

        try:
            token = _bearer(scope)
            info = verify_token(token) if token is not None else None
        except AuthUnavailable as e:
            # Cannot judge the token yet: refused, never passed on unchecked. The caller
            # gets the error's kind; the text is logged.
            from server.observability.metrics import record_auth_refusal

            record_auth_refusal(e, transport="http")
            kind = shown_unauthenticated(log, "tenancy middleware", e)
            message = f"this server cannot judge the token yet ({kind}); retry shortly"
            await _refuse(
                scope,
                receive,
                send,
                path,
                503,
                {"code": "AUTH_UNAVAILABLE", "message": message},
                {"Retry-After": "5"},
            )
            return
        except TokenError:  # a malformed header or a token that does not verify
            info = None
        if info is None:
            await self.app(scope, receive, send)  # the route answers it
            return
        try:
            org = acting_org(path, info)
            # The sign-up org a route falls back to is not the token's: no principal, so
            # it is recorded or refused, never placed, as for a sign-up.
            await tenancy.ensure_held(org, info if org == info.org else None)
        except LoomServerError as e:
            await _refuse(scope, receive, send, path, e.status_code, e.to_detail(), e.headers())
            return
        await self.app(scope, receive, send)


async def _refuse(
    scope: Scope,
    receive: Receive,
    send: Send,
    path: str,
    status: int,
    detail: dict[str, Any],
    headers: dict[str, str] | None,
) -> None:
    """A refusal in the shape the route's own clients parse: an RFC 7644 error on the
    SCIM routes, elsewhere Loom's error envelope with every field the app's handler
    gives the same error (`retry_after_s` and the rest)."""
    code = str(detail.get("code") or "")
    if path.startswith("/scim/"):
        from server.auth.scim_routes import scim_error

        response = scim_error(status, f"{code}: {detail.get('message', '')}")
    else:
        detail = classified(detail, status)
        # The help link every coded error carries from the app's handlers.
        linked = {**(headers or {}), "Link": error_help_link(code)}
        response = JSONResponse({"detail": detail}, status_code=status, headers=linked)
    await response(scope, receive, send)


def _bearer(scope: Scope) -> str | None:
    from server.auth.bearer import bearer_token

    for name, value in scope.get("headers") or ():
        if name == b"authorization":
            return bearer_token(value.decode("latin-1"))
    return None
