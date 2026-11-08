"""MCP request authentication and the pre-dispatch access screen: bearer
verification, the per-mode auth rule, the 401 / 503 answers with their
`WWW-Authenticate` challenge, and the org / namespace screen of an
authenticated `tools/call`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request, Response

from server.auth.fanout import knowledge_search_reads_collections
from server.code.domain.scope import set_code_scope
from server.core.errors import (
    IdentityIncompleteError,
    LoomServerError,
    classified,
    unverified_class,
)
from server.core.naming import present
from server.observability.consumer import ASSISTANT_CONSUMER
from server.operations.base import Role
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


async def _may_read(auth: AuthInfo, org: str, ns: str) -> tuple[bool, bool]:
    """(allowed, unverified): whether the gate admits `auth` to read (org, ns)
    — the claim, a grant (or an inheriting service account's creator's), the
    org-readable code rule and the restricted two-factor check, decided by
    `check_authorized` alone — and, for a refusal, whether it was made
    because a read it needed failed (server.auth.fail_closed): still
    refused, but the server's failure."""
    from server.auth.authz import check_authorized
    from server.operations.base import Role

    try:
        await check_authorized(auth, Role.VIEWER, org=org, namespace=ns)
    except PermissionError as e:
        return False, bool(getattr(e, "unverified", False))
    except IdentityIncompleteError:
        return False, False
    return True, False


# The next step for a namespace the token may not read. A namespace that does not exist
# is refused alike, so the step is the same for both and confirms neither.
NAMESPACE_NEXT = (
    "namespace.list_mine (the loom_namespace_list_mine tool, or through loom_op) lists the "
    "namespaces this token can use: check the namespace name against it before requesting "
    "a grant"
)


def _not_accessible_result(
    tool_name: Any,
    auth: Any,
    ns: str,
    *,
    assistant: bool = False,
    unverified: bool = False,
) -> dict[str, Any]:
    """A namespace the token may not read. `assistant`: the connection is the assistant
    toolset, which reaches neither loom_search nor loom_op, so neither is suggested.
    `unverified`: the check itself failed (a grant could not be read), so a collection's
    refusal asks for no grant: the caller may hold one and should retry. A plain
    namespace's refusal is the same either way."""
    from server.transport import access_refusal as ar

    if ar.is_plane_tool(str(tool_name or "")):
        # One answer whether or not the check ran: a restricted code namespace is refused
        # before the grant read that fails.
        subject, org = str(getattr(auth, "subject", "") or ""), str(getattr(auth, "org", ""))
        return ar.mcp_result(ar.refusal(subject, org, ns))
    from server.auth import sharing

    # Someone else's personal collection takes no grant: no advice could work. Any other
    # collection is shared by grants, never joined; the words are the same whether or not
    # it exists.
    not_listed = f"namespace {ns!r} is not in your accessible list."
    if sharing.is_personal(ns):
        return _refusal_body(not_listed, ns)
    if unverified and sharing.is_collection(ns):
        return _refusal_body(not_listed, ns, **unverified_class("NAMESPACE_NOT_ACCESSIBLE", True))
    creator = ""
    if sharing.is_collection(ns):
        # A delegated token's creator needs the role too, as the gate says.
        if getattr(auth, "creator", None):
            creator = " This token's creator must be active and hold a grant on it too."
        # A token its own claim caps is told both what it needs, from the token alone (what
        # its grants reach on a collection it cannot see would confirm the collection).
        needs = sharing.claim_cap(auth, auth.org, ns, Role.VIEWER)
        if needs:
            return _refusal_body(
                f"namespace {ns!r} is not in your accessible list: {needs}, and"
                f" {getattr(auth, 'subject', '')!r} must hold a grant of at least viewer on it or"
                f" a collection above it (directly, through a group or to everyone).{creator}",
                ns,
            )
    msg = (
        f"namespace {ns!r} is not in your accessible list and you don't have an active grant on it."
    )
    if assistant:
        msg += (
            f" Ask the collection's admin for a grant on it or a collection above it.{creator}"
            if sharing.is_collection(ns)
            else " Omit the namespace argument to use your default namespace, or request a "
            "grant from the dashboard's Spaces page."
        )
    elif sharing.is_collection(ns):
        # Never "omit the namespace": a write meant for the collection would land in the
        # default namespace instead.
        msg += (
            " Ask the collection's admin for a grant on it or a collection above it; the op "
            f"collection.list (through loom_op) lists the collections you can read.{creator}"
        )
    else:
        reads = (
            "your namespaces and the collections you can read"
            if knowledge_search_reads_collections()
            else "your namespaces"
        )
        msg += (
            f" Next: {NAMESPACE_NEXT}. Omit the namespace argument to use your default "
            f"namespace (loom_search reads {reads}), or request a grant from the dashboard's "
            f"Spaces page.{creator}"
        )
        return _refusal_body(msg, ns, next=NAMESPACE_NEXT)
    return _refusal_body(msg, ns)


def _refusal_body(msg: str, ns: str, **extra: Any) -> dict[str, Any]:
    body = {"error": msg, "code": "NAMESPACE_NOT_ACCESSIBLE", "namespace": ns, **extra}
    return {"content": [{"type": "text", "text": msg}], "isError": True, "structuredContent": body}


def _ledger_refusal(
    tool_name: Any,
    auth: AuthInfo,
    *,
    error_code: str,
    org: str = "",
    server_fault: bool = False,
    consumer: str = "",
) -> None:
    from server.transport.mcp_ledger import emit_token_refusal
    from server.transport.mcp_tools import TOOLS_BY_NAME

    op = TOOLS_BY_NAME.get(str(tool_name or ""))
    if op is not None:
        emit_token_refusal(
            op.name,
            auth,
            error_code=error_code,
            org=org,
            server_fault=server_fault,
            consumer=consumer,
        )


async def _plane_namespace(
    tool_name: Any, auth: Any, ns: str
) -> tuple[str | dict[str, Any] | None, bool]:
    """For a code/tracker tool's inaccessible `namespace`: the namespace it
    resolves to (and the resolution noted for the result),
    the UNKNOWN_NAMESPACE result when it names nothing, or None when it
    exists (the caller refuses as before). A resolution the caller cannot
    read either is refused on the real namespace. Beside it, whether that
    refusal was made because a read it needed failed."""
    from server.transport import access_refusal as ar
    from server.transport import namespace_resolve as nr

    if not ar.is_plane_tool(str(tool_name or "")):
        return None, False
    org = str(getattr(auth, "org", "") or "")
    known = nr.known_namespaces(org)
    if ns in known:
        return None, False
    to = nr.match(ns, known, default_branch=nr.default_branches(org))
    if to and to != ns:
        allowed, unverified = await _may_read(auth, org, to)
        if not allowed:
            return _not_accessible_result(tool_name, auth, to, unverified=unverified), unverified
        nr.RESOLVED.set({"from": ns, "to": to})
        return to, False
    return nr.unknown_result(ns, nr.suggest(ns, nr.suggestable(org, auth.can_access))), False


def _verify_bearer(request: Request) -> AuthInfo | None:
    """Verify the JWT on an MCP request.

    The bearer comes from the ``Authorization: Bearer <jwt>`` header only. A
    token in the URL is never read: request lines land in proxy and access
    logs, so a URL credential would be written to disk in the clear.

    Returns AuthInfo on success, None when the request carries no
    credential. Raises TokenError on a malformed or invalid one (the caller
    answers 401), and PermissionError for an end-user token (403: it is valid,
    and a 401 would make the client drop it).
    """
    # The checkout's code namespace, when the client's headers
    # helper named one; applied to namespace-less code.* calls. Read here, on
    # the same per-request path that reads the bearer.
    set_code_scope(request.headers.get("x-loom-code-scope"))
    from server.auth.bearer import bearer_token
    from server.auth.gateway import is_end_user
    from server.auth.jwt import verify_token

    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        return None
    auth = verify_token(token)
    if is_end_user(auth):
        # An end user's service calls the HTTP API, which maps `@me` and its downloads.
        raise PermissionError("an end-user token calls the HTTP API (/v1/<op>), not MCP")
    return auth


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
    if invalid_token_reason is None:  # a refused token was counted by _token_refused
        from server.observability.metrics import record_auth_refusal

        record_auth_refusal(None, transport="mcp")
    response.status_code = 401
    response.headers["WWW-Authenticate"] = _bearer_challenge(
        request, invalid_token_reason=invalid_token_reason
    )
    return _err(_echo_request_id(payload), INVALID_REQUEST, message)


def _not_on_mcp(response: Response, payload: Any, error: PermissionError) -> dict[str, Any]:
    """The 403 for a valid token MCP does not serve (an end-user token)."""
    from server.observability.metrics import record_auth_refusal

    record_auth_refusal(error, transport="mcp")
    response.status_code = 403
    return _err(_echo_request_id(payload), INVALID_REQUEST, str(error))


def _token_refused(
    request: Request, response: Response, payload: Any, error: Exception
) -> dict[str, Any]:
    """The answer to a bearer that failed verification. A revocation list that has
    not loaded yet is a 503 + Retry-After: the token may be good, and a 401 would
    make the client discard it and re-authenticate."""
    from server.auth.jwt import AuthUnavailable
    from server.observability.metrics import record_auth_refusal

    record_auth_refusal(error, transport="mcp")
    if isinstance(error, AuthUnavailable):
        response.status_code = 503
        response.headers["Retry-After"] = "5"
        data = classified({"code": "AUTH_UNAVAILABLE", "message": str(error)}, 503)
        return _err(_echo_request_id(payload), TOOL_EXEC_FAILED, str(error), data)
    return _reject_unauthenticated(
        request,
        response,
        payload,
        message=f"unauthenticated: {error}",
        invalid_token_reason=str(error),
    )


async def _screen_tool_call(
    req: dict[str, Any], auth: AuthInfo, *, consumer: str = ""
) -> dict[str, Any] | None:
    """The pre-dispatch screen of an authenticated `tools/call`: the call's
    `org` must be one the token may act in (ORG_MISMATCH), and a `namespace`
    the gate would not let it read is refused (NAMESPACE_NOT_ACCESSIBLE) — a
    code/tracker tool's resolves to the namespace it names, rewritten into
    the arguments in place. Returns the
    refusal response, or None to dispatch. A malformed `params` /
    `arguments` passes through: `_handle_request` answers it. `consumer`: the one the
    connection names, which a refusal row carries below a token's claim."""
    from server.transport import namespace_resolve as nr

    # The resolution note is this call's: a batch runs its entries in one
    # task, so the previous entry's would otherwise reach this result.
    nr.RESOLVED.set(None)
    params = req.get("params")
    args = params.get("arguments") if isinstance(params, dict) else None
    if not isinstance(params, dict) or not isinstance(args, dict):
        return None
    # A blank or "-" org or namespace names none, as the gates read it.
    req_org = present(args.get("org"))
    req_ns = present(args.get("namespace"))
    # loom_search addresses the caller's home and reads its own memory
    # namespaces (`server.auth.fanout`), so strip any namespace arg rather
    # than reject the call, and let the handler run with defaults.
    tool_name = params.get("name")
    if tool_name == _LOOM_SEARCH_TOOL_NAME and req_ns:
        args.pop("namespace", None)
        req_ns = None
    from server.auth.authz import may_act_in_org

    # The one cross-org rule (a platform admin may act in any org).
    if req_org and not may_act_in_org(auth, req_org):
        # result-with-isError, not a JSON-RPC error frame (clients
        # drop conversation state on those) — so the agent reads
        # the message and can recover. Ledgered in the token's org, never
        # the one the call named.
        _ledger_refusal(tool_name, auth, error_code="ORG_MISMATCH", consumer=consumer)
        msg = f"token org {auth.org!r} != request org {req_org!r}"
        return _ok(
            req.get("id"),
            {
                "content": [{"type": "text", "text": msg}],
                "isError": True,
                "structuredContent": {"error": msg, "code": "ORG_MISMATCH"},
            },
        )
    target_org = req_org or auth.org
    if not req_ns:
        return None

    try:
        return await _screen_namespace(req, tool_name, args, auth, target_org, req_ns, consumer)
    except LoomServerError as e:
        # A read the check could not make past its cache (a 503): counted
        # against the server, then raised.
        _ledger_refusal(tool_name, auth, error_code=e.code, org=target_org, consumer=consumer)
        raise


async def _screen_namespace(
    req: dict[str, Any],
    tool_name: Any,
    args: dict[str, Any],
    auth: AuthInfo,
    target_org: str,
    req_ns: str,
    consumer: str,
) -> dict[str, Any] | None:
    """The screen's namespace check: None when the caller may read `req_ns`
    (or the code/tracker index it resolves to, written into `args`), else
    the refusal, ledgered."""
    allowed, given_unverified = await _may_read(auth, target_org, req_ns)
    if not allowed:
        # A code/tracker tool's `namespace` that is not a namespace resolves
        # to one or says so — never the claim text meant for a real refusal.
        resolved, resolved_unverified = await _plane_namespace(tool_name, auth, req_ns)
        if isinstance(resolved, str):  # a namespace the caller may read
            args["namespace"] = resolved
            return None
        # The refusal to answer, and whether the check that made it was unverified.
        if isinstance(resolved, dict):  # on the name it resolved to, or an unknown name
            refusal, unverified = resolved, resolved_unverified
        else:  # on the name given
            refusal = _not_accessible_result(
                tool_name,
                auth,
                req_ns,
                assistant=consumer == ASSISTANT_CONSUMER,
                unverified=given_unverified,
            )
            unverified = given_unverified
        # The org the token may act in, as HTTP records the same refusal.
        code = str(refusal["structuredContent"]["code"])
        _ledger_refusal(
            tool_name,
            auth,
            error_code=code,
            org=target_org,
            server_fault=unverified,
            consumer=consumer,
        )
        return _ok(req.get("id"), refusal)
    # Readable: the op's own gate enforces the role it requires.
    return None
