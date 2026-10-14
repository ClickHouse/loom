"""MCP request authentication and the pre-dispatch access screen: bearer
verification, the per-mode auth rule, the 401 / 503 answers with their
`WWW-Authenticate` challenge, and the org / namespace screen of an
authenticated `tools/call`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request, Response

from server.code.domain.scope import set_code_scope
from server.transport.mcp_config import (
    _LOOM_SEARCH_TOOL_NAME,
    _TRANSPORT_ERROR_ID,
    INVALID_REQUEST,
    TOOL_EXEC_FAILED,
    _err,
    _ok,
)

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo


async def _may_read(auth: AuthInfo, org: str, ns: str) -> bool:
    """Whether the gate admits `auth` to read (org, ns): the claim, a grant
    (or an inheriting service account's creator's), the org-readable code
    rule and the restricted two-factor check, decided by `check_authorized`
    alone."""
    from server.auth.authz import check_authorized
    from server.core.errors import IdentityIncompleteError
    from server.operations.base import Role

    try:
        await check_authorized(auth, Role.VIEWER, org=org, namespace=ns)
    except (PermissionError, IdentityIncompleteError):
        return False
    return True


def _not_accessible_result(tool_name: Any, auth: Any, ns: str) -> dict[str, Any]:
    """A namespace the token may not read."""
    from server.transport import access_refusal as ar

    if ar.is_plane_tool(str(tool_name or "")):
        return ar.mcp_result(
            ar.refusal(str(getattr(auth, "subject", "") or ""), str(getattr(auth, "org", "")), ns)
        )
    msg = (
        f"namespace {ns!r} is not in your accessible list and you don't have an active grant "
        f"on it. Omit the namespace argument to use your default namespace (loom_search "
        f"reads your own namespaces), or request a grant via /me/discover."
    )
    body = {"error": msg, "code": "NAMESPACE_NOT_ACCESSIBLE", "namespace": ns}
    return {"content": [{"type": "text", "text": msg}], "isError": True, "structuredContent": body}


async def _plane_namespace(tool_name: Any, auth: Any, ns: str) -> str | dict[str, Any] | None:
    """For a code/tracker tool's inaccessible `namespace`: the namespace it
    resolves to (and the resolution noted for the result),
    the UNKNOWN_NAMESPACE result when it names nothing, or None when it
    exists (the caller refuses as before). A resolution the caller cannot
    read either is refused on the real namespace."""
    from server.transport import access_refusal as ar
    from server.transport import namespace_resolve as nr

    if not ar.is_plane_tool(str(tool_name or "")):
        return None
    org = str(getattr(auth, "org", "") or "")
    known = nr.known_namespaces(org)
    if ns in known:
        return None
    to = nr.match(ns, known, default_branch=nr.default_branches(org))
    if to and to != ns:
        if not await _may_read(auth, org, to):
            return _not_accessible_result(tool_name, auth, to)
        nr.RESOLVED.set({"from": ns, "to": to})
        return to
    return nr.unknown_result(ns, nr.suggest(ns, nr.suggestable(org, auth.can_access)))


def _verify_bearer(request: Request) -> AuthInfo | None:
    """Verify the JWT on an MCP request.

    The bearer comes from the ``Authorization: Bearer <jwt>`` header only. A
    token in the URL is never read: request lines land in proxy and access
    logs, so a URL credential would be written to disk in the clear.

    Returns AuthInfo on success, None when the request carries no
    credential. Raises TokenError on a malformed or invalid one (the caller
    answers 401).
    """
    # The checkout's code namespace, when the client's headers
    # helper named one; applied to namespace-less code.* calls. Read here, on
    # the same per-request path that reads the bearer.
    set_code_scope(request.headers.get("x-loom-code-scope"))
    from server.core.read_replica import remember_caller_headers

    remember_caller_headers(request.headers)
    from server.auth.bearer import bearer_token
    from server.auth.jwt import verify_token

    token = bearer_token(request.headers.get("authorization"))
    return None if token is None else verify_token(token)


def _requires_auth(method: str, mode: str) -> bool:
    """Whether `method` requires a verified bearer under the given auth mode.

    `mode` is one of:
      - "required":       every method requires a bearer (default).
      - "discovery_only": initialize / tools/list / ping are open; tools/call needs auth.
      - "open":           no method requires auth (trusted-network only).
    """
    if mode == "open":
        return False
    if mode == "discovery_only":
        return method == "tools/call"
    # "required" (default) — every observable method needs a bearer: the listings, the
    # handshake, `server/discover` (so it is never answered unauthenticated while
    # `initialize` is gated), the prompt and the primer resource. Only notifications,
    # which carry no answer, pass.
    return not method.startswith("notifications/")


def _oauth_challenge_header(request: Request) -> str:
    """`WWW-Authenticate` value pointing a native MCP connector at the
    protected-resource metadata, so it can discover the authorization server and
    start the browser OAuth sign-in. Same issuer logic as the discovery routes
    (`server.auth.oauth.issuer`) so the advertised URL can't drift."""
    from server.auth.oauth import issuer

    prm = f"{issuer(request)}/.well-known/oauth-protected-resource"
    return f'Bearer resource_metadata="{prm}"'


def _effective_auth_mode() -> str:
    """`mcp_auth_mode`, collapsed to "open" when the deployment runs without auth."""
    from server.settings import settings

    return settings.mcp_auth_mode if settings.auth_actually_required() else "open"


def _echo_request_id(payload: Any) -> str | int:
    """Id for a transport-level JSON-RPC error frame.

    MCP narrows `JSONRPCError.id` to string | number, so the reference
    TypeScript SDK's `JSONRPCMessageSchema.parse` rejects an `id: null` frame
    with a zod union error and the client never shows our message. Echo the
    first id the payload carried; fall back to a fixed string when it had none
    (a notification, or a body that isn't JSON-RPC at all).
    """
    reqs = payload if isinstance(payload, list) else [payload]
    for req in reqs:
        rid = req.get("id") if isinstance(req, dict) else None
        if isinstance(rid, (str, int)) and not isinstance(rid, bool):
            return rid
    return _TRANSPORT_ERROR_ID


def _first_method_requiring_auth(payload: Any, mode: str) -> str | None:
    """The first method in the payload that `mode` gates, or None."""
    reqs = payload if isinstance(payload, list) else [payload]
    for req in reqs:
        method = str(req.get("method", "") or "") if isinstance(req, dict) else ""
        if _requires_auth(method, mode):
            return method
    return None


def _bearer_challenge(request: Request, *, invalid_token_reason: str | None) -> str:
    """RFC 6750 `WWW-Authenticate` value for an MCP 401.

    Always carries `realm="loom"`. A present-but-invalid token adds
    `error="invalid_token"` + `error_description` (RFC 6750 §3.1); a missing
    token adds no error code. With OAuth on, the `resource_metadata` pointer
    stays so a native connector can still discover the authorization server.
    """
    from server.settings import settings

    params = ['realm="loom"']
    if settings.oauth_enabled:
        params.append(_oauth_challenge_header(request).removeprefix("Bearer "))
    if invalid_token_reason is not None:
        # quoted-string: printable, no quotes or backslashes.
        desc = "".join(ch for ch in invalid_token_reason if ch.isprintable() and ch not in '"\\')
        params.append('error="invalid_token"')
        params.append(f'error_description="{desc}"')
    return "Bearer " + ", ".join(params)


def _reject_unauthenticated(
    request: Request,
    response: Response,
    payload: Any,
    *,
    message: str,
    invalid_token_reason: str | None = None,
) -> dict[str, Any]:
    """401 + `WWW-Authenticate` + a JSON-RPC error frame whose id is never null.

    An authentication failure is an HTTP-level fact — the MCP Authorization
    spec has invalid or expired tokens answered with 401 — and does not depend
    on whether this deployment runs an authorization server.
    """
    response.status_code = 401
    response.headers["WWW-Authenticate"] = _bearer_challenge(
        request, invalid_token_reason=invalid_token_reason
    )
    return _err(_echo_request_id(payload), INVALID_REQUEST, message)


def _token_refused(
    request: Request, response: Response, payload: Any, error: Exception
) -> dict[str, Any]:
    """The answer to a bearer that failed verification. A revocation list that has
    not loaded yet is a 503 + Retry-After: the token may be good, and a 401 would
    make the client discard it and re-authenticate."""
    from server.auth.jwt import RevocationUnavailable

    if isinstance(error, RevocationUnavailable):
        response.status_code = 503
        response.headers["Retry-After"] = "5"
        return _err(_echo_request_id(payload), TOOL_EXEC_FAILED, str(error))
    return _reject_unauthenticated(
        request,
        response,
        payload,
        message=f"unauthenticated: {error}",
        invalid_token_reason=str(error),
    )


async def _screen_tool_call(req: dict[str, Any], auth: AuthInfo) -> dict[str, Any] | None:
    """The pre-dispatch screen of an authenticated `tools/call`: the call's
    `org` must be one the token may act in (ORG_MISMATCH), and a `namespace`
    the gate would not let it read is refused (NAMESPACE_NOT_ACCESSIBLE) — a
    code/tracker tool's resolves to the namespace it names, rewritten into
    the arguments in place. Returns the
    refusal response, or None to dispatch. A malformed `params` /
    `arguments` passes through: `_handle_request` answers it."""
    from server.transport import namespace_resolve as nr

    # The resolution note is this call's: a batch runs its entries in one
    # task, so the previous entry's would otherwise reach this result.
    nr.RESOLVED.set(None)
    params = req.get("params")
    args = params.get("arguments") if isinstance(params, dict) else None
    if not isinstance(params, dict) or not isinstance(args, dict):
        return None
    req_org = args.get("org")
    req_ns = args.get("namespace")
    # loom_search addresses the caller's home and reads its own memory
    # namespaces (`server.auth.fanout`), so strip any namespace arg rather
    # than reject the call, and let the handler run with defaults.
    tool_name = params.get("name")
    if tool_name == _LOOM_SEARCH_TOOL_NAME and req_ns:
        args.pop("namespace", None)
        req_ns = None
    from server.auth.authz import may_act_in_org

    # The one cross-org rule (a platform admin may act in any org).
    if req_org and not may_act_in_org(auth, str(req_org)):
        # result-with-isError, not a JSON-RPC error frame (clients
        # drop conversation state on those) — so the agent reads
        # the message and can recover.
        msg = f"token org {auth.org!r} != request org {req_org!r}"
        return _ok(
            req.get("id"),
            {
                "content": [{"type": "text", "text": msg}],
                "isError": True,
                "structuredContent": {"error": msg, "code": "ORG_MISMATCH"},
            },
        )
    target_org = str(req_org or auth.org)
    if req_ns and not await _may_read(auth, target_org, str(req_ns)):
        # A code/tracker tool's `namespace` that is not a namespace resolves
        # to one or says so — never the claim text meant for a real refusal.
        fixed = await _plane_namespace(tool_name, auth, req_ns)
        if isinstance(fixed, dict):
            return _ok(req.get("id"), fixed)
        if not fixed:
            return _ok(req.get("id"), _not_accessible_result(tool_name, auth, req_ns))
        args["namespace"] = fixed
    # Readable: the op's own gate enforces the role it requires.
    return None
