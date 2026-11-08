"""MCP transport — JSON-RPC over HTTP at `/mcp`.

Every operation in the registry becomes one MCP tool, auto-derived:
  • name        : op.mcp_tool_name     (e.g. `loom_memory_get`)
  • description : curated text in mcp_descriptions.TOOL_DESCRIPTIONS keyed
                  by op.name, else a generated metadata fallback
  • inputSchema : Pydantic-derived JSON Schema from op.input_model
  • outputSchema: Pydantic-derived JSON Schema from op.output_model

Streamable-HTTP request/response (no SSE; no server-initiated events).
Methods: `initialize`, `tools/list`, `tools/call`; notifications are
accepted and produce no response.

`tools/call` enforces the same role / namespace / rate-limit / audit /
budget checks as HTTP. MCP has no idempotency header, so callers may pass
`_idempotency_key` in tool arguments for replay semantics.

The endpoint and the JSON-RPC dispatch live here. The rest live in
one-directional modules (none import back into this one):
  • mcp_config    — constants + JSON-RPC envelope helpers
  • mcp_schemas   — JSON-Schema transformation for descriptors
  • mcp_identity  — client-name slugify / session→agent cache / caller defaults
  • mcp_tools     — tool descriptors, the loom_search rendering, the per-call ledger rows
  • mcp_ledger    — the rows of calls refused before their handler, once per batch
  • mcp_op        — the `loom_op` tool: an op by name, and the ones kept off MCP
  • mcp_primer    — the primer and the prompts / resources / initialize surfaces
  • mcp_auth      — bearer verification, 401 answers, the pre-dispatch screen
  • mcp_call      — one `tools/call`, from tool resolution to the handler
  • mcp_outcome   — the call's telemetry, result and failure answer
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response

from server.core.errors import classified
from server.identity.app.caller_home import caller_org
from server.observability.consumer import ASSISTANT_CONSUMER
from server.transport import gates
from server.transport import mcp_runs as _mcp_runs
from server.transport.mcp_auth import (
    _echo_request_id,
    _effective_auth_mode,
    _first_method_requiring_auth,
    _not_on_mcp,
    _reject_unauthenticated,
    _screen_tool_call,
    _token_refused,
    _verify_bearer,
)
from server.transport.mcp_call import _call_tool
from server.transport.mcp_config import (
    _TRANSPORT_ERROR_ID,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LIST_TTL_MS,
    MAX_BATCH_ENTRIES,
    MCP_SUPPORTED_PROTOCOL_VERSIONS,
    META_SERVER_INFO,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_INFO,
    _err,
    _ok,
    cache_hints,
    negotiate_protocol_version,
    protocol_version_supported,
    requested_meta_version,
    unsupported_version_error,
)
from server.transport.mcp_identity import (
    _MCP_AGENT_BY_SESSION,
    _client_name_to_agent,
    _remember_client_for_session,
    _resolve_client_name,
    new_session_id,
)
from server.transport.mcp_op import route as route_loom_op
from server.transport.mcp_primer import (
    _emit_initialize_event,
    _get_prompt,
    _initialize_instructions,
    _prompts_descriptor,
    _read_resource,
    _resources_descriptor,
)
from server.transport.mcp_refusals import classify_tool_errors
from server.transport.mcp_tools import _tools_descriptor, assistant_connection

log = logging.getLogger(__name__)


# ============================================================
# JSON-RPC dispatch
# ============================================================


def _listing_params(request: Any) -> dict[str, str | None]:
    """The per-request `tools/list` selectors on the MCP URL:
    `?schema_profile=` and `?toolset=`."""
    return {
        "schema_profile": request.query_params.get("schema_profile"),
        "toolset": request.query_params.get("toolset"),
    }


async def _handle_request(
    payload: dict[str, Any],
    auth: Any = None,
    client_name: str | None = None,
    mcp_session_id: str = "",
    schema_profile: str | None = None,
    toolset: str | None = None,
) -> dict[str, Any] | None:
    """Process one JSON-RPC request. Returns response or None (notification).

    ``client_name`` is the resolved MCP `clientInfo.name` for this request
    (inline on `initialize`, else cached against `Mcp-Session-Id`). Threaded
    into the write/observability paths so provenance is attributed to the
    actual client rather than the default agent.

    ``mcp_session_id`` is the server-assigned `Mcp-Session-Id`, threaded into
    the observability layer so MCP traces carry a session even when the caller
    passes no payload `session_id`, and one agent's conversation stays
    navigable.
    """
    if not isinstance(payload, dict):
        return _err(_TRANSPORT_ERROR_ID, INVALID_REQUEST, "Request must be a JSON object")

    req_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return _err(req_id, INVALID_PARAMS, f"{method} 'params' must be an object")

    # A 2026-07-28 client declares its version on every request. One this
    # transport does not serve gets the recognised modern error naming the
    # versions it can retry with — except `server/discover`, which exists to
    # be answered before the client has picked a version.
    meta_version = requested_meta_version(payload)
    if (
        meta_version is not None
        and method != "server/discover"
        and not protocol_version_supported(meta_version)
    ):
        return unsupported_version_error(req_id, meta_version)

    if method == "server/discover":
        return _ok(req_id, await _discover_result(auth, toolset))

    if method == "initialize":
        result: dict[str, Any] = {
            # The client's version when this transport serves it, else the
            # latest it serves — the lifecycle spec's negotiation rule.
            "protocolVersion": negotiate_protocol_version(params.get("protocolVersion")),
            "serverInfo": SERVER_INFO,
            # Four surfaces for the primer (clients differ in what they
            # support): `instructions` here (zero-action session-start
            # channel, `_initialize_instructions`), the `loom://primer`
            # resource, the `loom` prompt, and the `loom_activate` tool.
            "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
        }
        try:
            instructions = await _initialize_instructions(auth, toolset)
        except Exception as e:  # noqa: BLE001 — the handshake must not fail on a primer render
            # Enhancement only — the handshake must not fail on a render hiccup.
            log.warning("initialize instructions render failed (non-fatal): %s", e)
            instructions = ""
        if instructions:
            result["instructions"] = instructions
            # The handshake is a profile read — the primer
            # inlines the caller's profile sections — so it leaves a ledger
            # row like `loom_activate` does, and the Profile page's "read by"
            # chip stops saying "never read" to a client that reads it on
            # every connect. Anonymous probes carry no profile and no row, and neither
            # does the assistant toolset's primer, which inlines no profile.
            if auth is not None and not assistant_connection(toolset):
                await _emit_initialize_event(auth, client_name)
        return _ok(req_id, result)

    # Notifications (no response by JSON-RPC spec)
    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        return None
    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        # `?schema_profile=compact` on the MCP URL selects the compact listing
        # (see mcp_schemas.compact_descriptor); `?toolset=coding` selects a
        # toolset — one deployment serves both archetypes.
        # Defaults from settings; an unknown value is the default.
        return _ok(
            req_id,
            {
                "tools": await _tools_descriptor(schema_profile=schema_profile, toolset=toolset),
                # The listing is a property of the deployment and the URL, not
                # of the caller: a client may hold it for five
                # minutes and a shared cache may keep it.
                **cache_hints(LIST_TTL_MS, "public"),
            },
        )

    if method == "tools/call":
        from server.transport.lean_results import single_channel

        return single_channel(
            classify_tool_errors(
                await _call_tool(
                    req_id,
                    params,
                    auth,
                    client_name,
                    mcp_session_id=mcp_session_id,
                    toolset=toolset,
                )
            ),
            _client_name_to_agent(client_name),
        )

    if method == "prompts/list":
        return _ok(
            req_id, {"prompts": _prompts_descriptor(toolset), **cache_hints(LIST_TTL_MS, "public")}
        )

    if method == "prompts/get":
        return await _get_prompt(req_id, params, auth, client_name, toolset)

    if method == "resources/list":
        return _ok(
            req_id,
            {"resources": _resources_descriptor(toolset), **cache_hints(LIST_TTL_MS, "public")},
        )

    if method == "resources/read":
        return await _read_resource(req_id, params, auth, toolset)

    return _err(req_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


async def _discover_result(auth: Any, toolset: str | None = None) -> dict[str, Any]:
    """`server/discover` (2026-07-28): the versions this
    transport serves, its capabilities and identity, and the same primer the
    `initialize` result carries — so a client that probes before choosing a
    version learns it must open with `initialize` on one of these, and a
    client that only wants to display the server needs no handshake."""
    result: dict[str, Any] = {
        "resultType": "complete",
        "supportedVersions": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
        "_meta": {META_SERVER_INFO: SERVER_INFO},
        # The instructions are the caller's (profile, namespaces): private.
        **cache_hints(LIST_TTL_MS, "private"),
    }
    try:
        instructions = await _initialize_instructions(auth, toolset)
    except Exception as e:  # noqa: BLE001 — discover must not fail on a primer render
        log.warning("discover instructions render failed (non-fatal): %s", e)
        instructions = ""
    if instructions:
        result["instructions"] = instructions
    return result


def _initialize_client_name(req: dict[str, Any]) -> str | None:
    params = req.get("params")
    info = params.get("clientInfo") if isinstance(params, dict) else None
    name = info.get("name") if isinstance(info, dict) else None
    return name if isinstance(name, str) else None


def _accepted(response: Response) -> Response:
    """202 with an empty body — the Streamable HTTP answer to a notification.
    FastAPI does not merge the injected response's headers
    onto a directly returned Response, so `Mcp-Session-Id` is copied over."""
    return Response(status_code=202, headers=dict(response.headers))


async def _dispatch_batch(
    payload: list[Any], dispatch: Callable[[Any], Awaitable[dict[str, Any] | None]]
) -> list[dict[str, Any]]:
    """Each entry of a JSON-RPC batch in turn, its answers in order.

    One span per entry: all entries share a single FastAPI server span, and
    the per-call telemetry stamps loom.* / loom.search.* onto the *active*
    span, so without a child span each call would overwrite the previous
    one's attributes and only the last would survive (a batch whose first
    search abstains would be recorded as not abstaining). A refusal the
    entries repeat is ledgered once (`mcp_ledger.one_refusal_row_each`)."""
    from server.observability import genai
    from server.observability.otel import traced
    from server.transport.mcp_ledger import one_refusal_row_each

    responses: list[dict[str, Any]] = []
    with one_refusal_row_each():
        for i, req in enumerate(payload):
            method = str(req.get("method", "") or "") if isinstance(req, dict) else ""
            # The entries share one task, and the Langfuse span processor stamps
            # the published identity on every span: drop the previous entry's
            # before this entry's span opens. Each entry publishes its own.
            genai.set_identity()
            with traced("mcp.batch.entry", rpc_method=method, batch_index=i):
                resp = await dispatch(req)
            if resp is not None:
                responses.append(resp)
    return responses


def attach(app: FastAPI) -> None:
    router = APIRouter()

    @router.post("/mcp", summary="MCP JSON-RPC endpoint (single request or batch)")
    async def mcp_endpoint(request: Request, response: Response) -> Any:
        from server.auth.jwt import TokenError

        # `Mcp-Session-Id` correlates the `initialize` handshake (carrying
        # `clientInfo.name`) with later `tools/call` POSTs. Assigned at initialize
        # when the client sent none (in _dispatch) and echoed on every answer this
        # endpoint returns, refusals included, so clients keep sending it — that
        # round-trip drives the provenance cache.
        session_id = request.headers.get("mcp-session-id")
        if session_id:
            response.headers["Mcp-Session-Id"] = session_id

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — any body read failure answers PARSE_ERROR
            return _err(_TRANSPORT_ERROR_ID, PARSE_ERROR, "Body is not valid JSON")

        # `MCP-Protocol-Version` (2025-06-18+): a version this transport does
        # not serve is a 400 with the recognised modern error naming the ones
        # it does. Absent, the spec has the server assume
        # 2025-03-26 — served here as every other version is.
        header_version = request.headers.get("mcp-protocol-version")
        if header_version is not None and not protocol_version_supported(header_version):
            response.status_code = 400
            return unsupported_version_error(_echo_request_id(payload), header_version)
        if isinstance(payload, list) and not 0 < len(payload) <= MAX_BATCH_ENTRIES:
            # Refused whole, before the bearer or any entry (JSON-RPC: an empty batch is invalid).
            return _err(
                _TRANSPORT_ERROR_ID,
                INVALID_REQUEST,
                f"A batch holds 1 to {MAX_BATCH_ENTRIES} requests; send larger work in parts",
            )

        # Verify the bearer once per request (single or batch). In open mode tolerate a
        # stale/malformed bearer — clients template Authorization from env vars that may
        # not resolve in demo setups. Every other mode answers an invalid or missing
        # bearer with HTTP 401 + WWW-Authenticate: the reference SDK reports 401 as
        # "needs authentication", while a 200 carrying an id:null error frame fails its
        # schema parse and surfaces as an opaque zod error.
        mode = _effective_auth_mode()
        try:
            auth = _verify_bearer(request)
        except PermissionError as e:
            return _not_on_mcp(response, payload, e)
        except TokenError as e:
            if mode != "open":
                return _token_refused(request, response, payload, e)
            # Open mode falls back to no-auth rather than 401 (so bridges
            # with mis-templated headers still work). Warn-log it — silent
            # fallback to the single-user sentinel is baffling to debug.
            log.warning(
                "mcp: bearer verification failed in open mode "
                "(falling back to no-auth, request will resolve "
                "as the single-user sentinel): %s",
                e,
            )
            auth = None
        gated = _first_method_requiring_auth(payload, mode) if auth is None else None
        if gated is not None:
            return _reject_unauthenticated(
                request,
                response,
                payload,
                message=f"missing bearer token ({gated} requires auth in mcp_auth_mode={mode!r})",
            )

        async def _dispatch(req: Any) -> dict[str, Any] | None:
            nonlocal session_id
            if not isinstance(req, dict):
                return await _handle_request(req)  # Invalid Request, per entry
            method = req.get("method", "")
            # Capture the client's self-reported name from the initialize
            # handshake and cache it against this session so later
            # tools/call POSTs (which don't repeat clientInfo) inherit it.
            if method == "initialize":
                # Session ids are server-assigned at initialize; without
                # issuing one here clients never send the header back and the
                # session→agent cache can never be read.
                if not session_id:
                    session_id = new_session_id(_initialize_client_name(req))
                response.headers["Mcp-Session-Id"] = session_id
                params = req.get("params")
                if isinstance(params, dict):
                    client_info = params.get("clientInfo")
                    if isinstance(client_info, dict):
                        _remember_client_for_session(session_id, client_info.get("name"))
            client_name = _resolve_client_name(req, session_id)
            # One read of the selectors, so routing, the screen and the call agree.
            listing = _listing_params(request)
            req, early = route_loom_op(req, toolset=listing["toolset"], auth=auth)
            if early is not None:
                return early  # classified where its text is rendered (mcp_op._result)
            # AuthZ: identity must match the args; a namespace the JWT lacks → grants.
            if auth is not None and req.get("method") == "tools/call":
                # An assistant connection's refusals are its own, as its calls are.
                assistant = assistant_connection(listing["toolset"])
                screened = await _screen_tool_call(
                    req, auth, consumer=ASSISTANT_CONSUMER if assistant else ""
                )
                if screened is not None:
                    return classify_tool_errors(screened)
            return await _handle_request(
                req,
                auth,
                client_name,
                mcp_session_id=session_id or "",
                **listing,
            )

        if isinstance(payload, list):
            responses = await _dispatch_batch(payload, _dispatch)
            return responses if responses else _accepted(response)

        resp = await _dispatch(payload)
        return resp if resp is not None else _accepted(response)

    @router.delete("/mcp", summary="MCP session end: close the session's run")
    async def mcp_session_end(request: Request, response: Response) -> Any:
        """The Streamable HTTP transport lets a client end its session with
        `DELETE` + `Mcp-Session-Id`. The session's implicit run closes here
        (status completed, outcome none); the `mcp_session_run_sweep` worker
        closes the ones that go idle. A DELETE costs one indexed read of the run
        by (org, id), plus a close when it finds one, under the caller's
        ClickHouse quota key; it draws the rate limiter like a run.complete call,
        so a loop of them is answered 429 instead of reaching ClickHouse."""
        from server.auth.jwt import TokenError

        mode = _effective_auth_mode()
        try:
            auth = _verify_bearer(request)
        except PermissionError as e:
            return _not_on_mcp(response, None, e)
        except TokenError as e:
            if mode != "open":
                return _token_refused(request, response, None, e)
            auth = None
        if mode != "open" and auth is None:
            return _reject_unauthenticated(request, response, None, message="missing bearer token")
        from server.auth.context import set_current_auth

        set_current_auth(auth)  # the run's close counts against the caller's quota
        org = caller_org(auth) or ""
        rl = gates.session_end_rate_limit(auth, org)
        if rl is not None and not rl.allowed:
            wait = gates.retry_after_s(rl)
            response.status_code = 429
            response.headers["Retry-After"] = str(wait)
            msg = f"MCP session end rate limit exceeded (retry in {wait}s)"
            return classified({"error": msg, "code": "RATE_LIMITED", "retry_after_s": wait}, 429)
        session_id = request.headers.get("mcp-session-id")
        closed = await _mcp_runs.close_session(session_id, org, _mcp_runs.subject_of(auth))
        if session_id:
            _MCP_AGENT_BY_SESSION.pop(session_id, None)
        return {"session_id": session_id or "", "run_closed": closed}

    app.include_router(router)
