"""HTTP/JSON transport — auto-generated from the operation registry.

Each request flow:
    1. Extract + verify bearer JWT if present (required when auth_required=True)
    2. Parse + validate body (Pydantic via op.input_model)
    3. Authz: role + namespace ACL check (if auth present + payload has identity)
    4. Run handler
    5. Emit one row to loom.agent_events (success or failure)
    6. Return response with X-Request-Id / X-Schema-Version / Server-Timing

Error codes are listed in docs/reference/errors.md.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import ValidationError

from server.auth.authz import ActedForRevoked
from server.auth.bearer import missing_bearer, verify_bearer_header
from server.auth.fail_closed import answered_unverified
from server.auth.jwt import AuthInfo
from server.code.app.common import (
    schedule_code_trace_validation_failure,
)
from server.core import idempotency, llm_free
from server.core.errors import (
    AuthInsufficientError,
    IdentityIncompleteError,
    LoomServerError,
    handler_failure_detail,
    unverified_class,
)
from server.core.naming import present
from server.observability.consumer import (
    bind_current_consumer,
    explicit_consumer,
    set_current_consumer,
)
from server.observability.ledger import current_trace_id, emit_agent_event, set_hint_context
from server.observability.traffic_class import (
    bind_claimed_class,
    current_traffic_class,
    header_value,
    set_current_traffic_class,
)
from server.operations.base import Operation
from server.operations.capabilities import SCHEMA_VERSION
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings
from server.transport import access_refusal as ar
from server.transport import gates
from server.transport._common import (
    ANONYMOUS_ACTOR,
    bind_request,
    ledger_identity,
    request_id_from,
    request_identity,
    sli_columns,
    validation_errors,
    validation_note,
)
from server.transport.arg_check import header_value as arg_header_value
from server.transport.arg_check import named_argument_error
from server.transport.arg_check import response_body as arg_response_body
from server.transport.post_rows import post_handler_rows

SENTINEL = "-"

log = logging.getLogger("server.transport.http")


# An op model's nested models are referenced from the document's
# components/schemas: the `#/$defs/...` pointers Pydantic writes by default
# resolve against the document root, where there is no `$defs`, and ReDoc
# refuses the spec.
_REF_TEMPLATE = "#/components/schemas/{model}"


def _op_schema(model: type[Any], components: dict[str, Any]) -> dict[str, Any]:
    """`model`'s JSON Schema with its nested models moved into `components`."""
    schema: dict[str, Any] = model.model_json_schema(ref_template=_REF_TEMPLATE)
    for name, sub in schema.pop("$defs", {}).items():
        if components.setdefault(name, sub) != sub:
            raise RuntimeError(f"two op models named {name!r} have different schemas")
    return schema


def build_router(components: dict[str, Any] | None = None) -> APIRouter:
    """One POST route per op. The nested models its schemas reference are
    collected into `components` (the OpenAPI components/schemas)."""
    components = {} if components is None else components
    router = APIRouter()
    for op in ALL_OPERATIONS:
        router.add_api_route(
            op.http_path,
            _make_endpoint(op),
            methods=["POST"],
            name=op.name,
            summary=f"{op.name} — budget p99 {op.budget_p99_ms}ms",
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": _op_schema(op.input_model, components)}
                    },
                },
                "responses": {
                    "200": {
                        "description": "Success",
                        "content": {
                            "application/json": {"schema": _op_schema(op.output_model, components)}
                        },
                    }
                },
            },
        )
    return router


def _extract_auth(request: Request) -> AuthInfo | None:
    """Pull a bearer token from the Authorization header and verify it.

    Returns None if no token is present (allowed only when auth_required=False).
    Raises HTTPException(401) on present-but-invalid tokens, or on missing
    tokens when auth_required=True.
    """
    # Counts its own refusals; an end-user token is served here.
    auth = verify_bearer_header(request.headers.get("Authorization"), acts_for_end_users=True)
    if auth is None:
        if settings.auth_actually_required():
            raise missing_bearer()
        return None

    from server.auth.gateway import effective_auth

    try:
        return effective_auth(auth, request.headers.get("X-Loom-On-Behalf-Of") or "")
    except PermissionError as e:
        raise _refused_assertion(e) from e


def _refused_assertion(e: PermissionError) -> HTTPException:
    """403 for an `X-Loom-On-Behalf-Of` the bearer may not send, counted as an auth refusal."""
    from server.observability.metrics import record_auth_refusal

    record_auth_refusal(e, transport="http")
    return HTTPException(status_code=403, detail={"code": "AUTH_INSUFFICIENT", "message": str(e)})


def _resolve_personal(
    op: Operation, call: _HttpCall, payload: Any, raw_body: dict[str, Any], auth: AuthInfo | None
) -> tuple[Any, dict[str, Any]]:
    """`namespace: "@me"` is the asserted end user's personal collection, which
    only the server names; any other caller is refused. The gate then
    authorizes it as any namespace."""
    from server.auth.gateway import personal_namespace
    from server.auth.sharing import PERSONAL_ALIAS
    from server.core.errors import ValidationError as RequestInvalid

    if getattr(payload, "namespace", None) != PERSONAL_ALIAS:
        return payload, raw_body
    org = getattr(payload, "org", None) or (auth.org if auth is not None else "")
    try:
        namespace = personal_namespace(auth, org, PERSONAL_ALIAS)
    except ValueError as e:
        raise _refuse(
            op, call, 400, error_code="VALIDATION_FAILED", e=RequestInvalid(str(e))
        ) from e
    return payload.model_copy(update={"namespace": namespace}), {**raw_body, "namespace": namespace}


def server_timing_header(elapsed_ms: int) -> str:
    """`Server-Timing` for this request: `app` is the
    handler's wall time; `ch` is the ClickHouse time the work bag summed
    from every statement's summary, with the statement count and bytes read
    in its description. Absent bag → `app` alone. Never raises."""
    parts = [f"app;dur={int(elapsed_ms)}"]
    try:
        from server.core.request_work import current

        bag = current()
        if bag is not None and bag.ch_statements:
            mb = bag.ch_read_bytes / 1e6
            size = f"{mb:.1f} MB" if mb >= 1 else f"{bag.ch_read_bytes / 1e3:.0f} kB"
            n = bag.ch_statements
            plural = "s" if n != 1 else ""
            parts.append(f'ch;dur={bag.ch_elapsed_ms:.1f};desc="{n} statement{plural}, {size}"')
    except Exception:  # noqa: BLE001 — a header is never worth a failed response
        pass
    return ", ".join(parts)


def _stamp_timing(response: Response, elapsed_ms: int) -> None:
    """`Server-Timing` on a successful answer, which the browser's network
    panel renders and the SDKs read: handler time, and the ClickHouse time
    and bytes the work bag summed."""
    response.headers["Server-Timing"] = server_timing_header(elapsed_ms)


@dataclass(slots=True)
class _HttpCall:
    """What one request's phases hand each other: its id, the parsed body,
    the verified token (None when anonymous) and the tenant triple read from
    the body (authorization fills in the code/tracker plane's defaults)."""

    request_id: str
    raw_body: dict[str, Any] = field(default_factory=dict)
    auth: AuthInfo | None = None
    identity: dict[str, str] = field(default_factory=dict)
    target: gates.Target | None = None


def _begin_request(op: Operation, request: Request, response: Response) -> str:
    """The request id, the schema header and the per-request context vars —
    before the body is read. Returns the request id."""
    request_id = request_id_from(request.headers.get("X-Request-Id"))
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Schema-Version"] = SCHEMA_VERSION
    bind_request(op.name, request_id)

    # Fresh per-op cost accumulator so one op's spend doesn't leak into
    # another's tally; _emit_event drains it at op end.
    from server.transport._common import begin_request

    begin_request()  # + degradation notes and the work bag

    # The consumer column defaults to the token's subject
    # class; before the token is verified only the body's own value is
    # known. Reset per request so a worker-less process never carries
    # the previous request's value forward.
    set_current_consumer("")
    set_current_traffic_class("")
    return request_id


async def _read_body(
    op: Operation, request: Request, request_id: str, auth: AuthInfo | None
) -> dict[str, Any]:
    """The JSON object body, or a 400 (not JSON); JSON that is not an object
    is the op's validation 422, and a field removed from the wire is a named
    422."""
    try:
        parsed = await request.json()
    except Exception as e:
        _emit_event(op, request_id, {}, 0, success=False, error_code="VALIDATION_FAILED", error=e)
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_FAILED", "message": "Body is not valid JSON"},
        ) from e
    if not isinstance(parsed, dict):
        # Every op's input is an object, so the model refuses it; the ledger
        # and identity readers here and below read the body as a dict.
        _validate_payload(op, request_id, parsed, auth)
    raw_body: dict[str, Any] = parsed

    set_current_consumer(explicit_consumer(raw_body))
    # A write's misspelled argument is refused by name, not dropped.
    refused = named_argument_error(op, raw_body)
    if refused is not None:
        _emit_event(
            op,
            request_id,
            _refused_body(raw_body, auth),
            0,
            success=False,
            error_code=refused.error_detail()["code"],
        )
        raise HTTPException(status_code=422, detail=refused.error_detail())
    return raw_body


async def _idempotency_gate(
    op: Operation,
    request: Request,
    response: Response,
    call: _HttpCall,
    target: gates.Target,
    payload: Any,
) -> gates.Replay:
    """The replay decision (`gates.idempotency_check`) for the call's
    Idempotency-Key, rendered: a key reused with another body is a 409, one
    whose first request still runs a retryable 503; a stored answer replays
    with its headers."""
    request_id, raw_body = call.request_id, call.raw_body
    start = time.perf_counter()
    replay = await gates.idempotency_check(
        op, call.auth, target, request.headers.get("Idempotency-Key") or "", payload
    )
    if replay.conflict:
        # Each code a literal: the errors reference finds the codes the server answers by them.
        if replay.in_progress:
            _emit_event(
                op, request_id, raw_body, 0, success=False, error_code="IDEMPOTENT_IN_PROGRESS"
            )
            # A 503, so every client that retries 5xx after Retry-After
            # gets the stored answer.
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "IDEMPOTENT_IN_PROGRESS",
                    "message": gates.IN_PROGRESS_MESSAGE,
                    "in_progress": True,
                },
                headers={"Retry-After": "1"},
            )
        _emit_event(op, request_id, raw_body, 0, success=False, error_code="IDEMPOTENT_CONFLICT")
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENT_CONFLICT",
                "message": "Idempotency-Key reused with different body",
            },
        )
    if replay.hit is not None:
        for hk, hv in replay.hit.headers.items():
            response.headers[hk] = hv
        response.headers["X-Idempotent-Replay"] = "true"
        # The lookup that answered it, as a handler's run is timed.
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        _emit_event(op, request_id, raw_body, elapsed_ms, success=True, error_code="")
    return replay


def _validate_payload(op: Operation, request_id: str, raw_body: Any, auth: AuthInfo | None) -> Any:
    """The body as the op's input model, or a 422 naming the failing field.
    `auth` is the verified token: a refusal naming another org is recorded,
    and traced, in the token's org."""
    try:
        payload = op.input_model.model_validate(raw_body)
    except ValidationError as e:
        # The row names the failing field, and a code.*
        # request leaves a trace row with error_code / error_text.
        errors = validation_errors(e)
        body = _refused_body(raw_body, auth) if isinstance(raw_body, dict) else {}
        _emit_event(
            op,
            request_id,
            body,
            0,
            success=False,
            error_code="VALIDATION_FAILED",
            error=validation_note(errors),
        )
        schedule_code_trace_validation_failure(op.name, body, errors)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "Input validation failed",
                "errors": errors,
            },
        ) from e
    return payload


def _refused_body(raw_body: dict[str, Any], auth: AuthInfo | None) -> dict[str, Any]:
    """The body a refusal before authorization is ledgered with: one naming an
    org the token may not act in is recorded in the token's org with
    namespace `-`, never the named one."""
    from server.auth.authz import may_act_in_org

    named = present(raw_body.get("org"))
    if auth is not None and named is not None and not may_act_in_org(auth, named):
        return {**raw_body, "org": auth.org, "namespace": SENTINEL}
    return raw_body


def _refuse(
    op: Operation,
    call: _HttpCall,
    status: int,
    *,
    error_code: str,
    e: BaseException | None = None,
    **extra: Any,
) -> HTTPException:
    """Record a gate's refusal on the ledger and build its HTTP error."""
    body = _refused_body(call.raw_body, call.auth)
    _emit_event(op, call.request_id, body, 0, success=False, error_code=error_code, error=e)
    detail: dict[str, Any] = {"code": error_code, "message": str(e) if e is not None else ""}
    detail.update(extra.pop("detail", {}))
    return HTTPException(status_code=status, detail=detail, **extra)


async def _authorize(
    op: Operation, response: Response, call: _HttpCall, payload: Any
) -> gates.Target:
    """The authorization target — the validated input's org and namespace,
    which the handler reads, as MCP authorizes them — then (for an
    authenticated request, fail closed) the role + namespace gate — 400
    IDENTITY_INCOMPLETE / 403 AUTH_INSUFFICIENT — and what an admitted call
    leaves, then the traffic class. Fills the resolved org and namespace
    into `call.identity`, as the handler will default them."""
    auth, identity = call.auth, call.identity
    try:
        target = gates.authz_target(
            op,
            org=getattr(payload, "org", None),
            namespace=getattr(payload, "namespace", None),
            auth=auth,
        )
        identity["org"] = target.org or SENTINEL
        identity["namespace"] = target.register_namespace or SENTINEL
        await gates.authorize(op, auth, target, transport="http")
    except IdentityIncompleteError as e:
        # NOT a permissions failure — the request did not say which
        # namespace or org it is for.
        raise _refuse(op, call, 400, error_code="IDENTITY_INCOMPLETE", e=e) from e
    except PermissionError as e:
        # A code/tracker index says why, as MCP's refusal does; a revoked subject a
        # trusted caller acts for is not an index's refusal.
        ns = target.namespace
        indexed = ar.is_plane_op(op.name) and not isinstance(e, ActedForRevoked)
        unverified = bool(getattr(e, "unverified", False))
        why: dict[str, object] = (
            ar.reason_fields(ns, unverified=unverified)
            if ns and indexed
            else unverified_class(gates.ROLE_REFUSAL_CODE, unverified)
        )
        raise _refuse(op, call, 403, error_code=gates.ROLE_REFUSAL_CODE, e=e, detail=why) from e
    except LoomServerError as e:
        # A read the gate could not make past its cache (a 503): counted
        # against the server, then answered as raised.
        body = _refused_body(call.raw_body, auth)
        _emit_event(op, call.request_id, body, 0, success=False, error_code=e.code, error=e)
        raise
    await gates.traffic_class(op, auth, target, call.raw_body)
    response.headers["X-Loom-Traffic-Class"] = header_value()
    return target


def _rate_limit(op: Operation, response: Response, call: _HttpCall, target: gates.Target) -> None:
    """The per-namespace / org / actor buckets, after authorization — a 429
    when one is empty. A request that names no namespace skips the limiter;
    a limiter that fails admits the call (gates.rate_limit)."""
    rl = gates.rate_limit(op, call.auth, target, current_traffic_class())
    if rl is None:
        return
    # Always populate the budget headers — they help SDKs pace.
    headers = {
        "X-RateLimit-Limit": str(int(rl.limit)),
        "X-RateLimit-Remaining": str(int(rl.remaining)),
        "X-RateLimit-Reset": str(max(1, int(rl.reset_s + 0.99))),
        "X-Loom-Traffic-Class": header_value(),
    }
    response.headers.update(headers)
    if not rl.allowed:
        wait = gates.retry_after_s(rl)
        raise _refuse(
            op,
            call,
            429,
            error_code="RATE_LIMITED",
            detail={
                "message": f"namespace {target.named_namespace!r} exceeded {op.name} rate limit",
                "retry_after_s": wait,
            },
            headers={"Retry-After": str(wait), **headers},
        )


async def _bind_context_and_policy(
    op: Operation, request: Request, call: _HttpCall, target: gates.Target, payload: Any
) -> Any:
    """The verified caller on `request.state` and the ambient run id,
    then the read scope, then the history policy gate (a structured 403 when
    it denies) on the scoped input. Returns the input the handler runs with."""
    request_id, raw_body, auth = call.request_id, call.raw_body, call.auth
    # For handlers that take Request; the ContextVar is bound at verification.
    request.state.auth = auth
    # The SDK's ambient run labels reads through a header rather than the
    # body's `run_id` (which would filter the read to the run's own rows).
    # Bound to this request's context; the ledger rows (agent_events,
    # memory_exposures) fall back to it.
    from server.observability.run_context import set_ledger_run

    set_ledger_run(request.headers.get("X-Loom-Run-Id") or "")

    # Read scope first: a body naming another user than the token's pin is refused
    # before any rule is evaluated for that user, and the policy sees the pinned input.
    try:
        payload = gates.read_scope(op, auth, payload)
    except IdentityIncompleteError as e:
        raise _refuse(op, call, 400, error_code="IDENTITY_INCOMPLETE", e=e) from e
    except AuthInsufficientError as e:
        raise _refuse(op, call, 403, error_code=gates.ROLE_REFUSAL_CODE, e=e) from e
    # History policy: after the role check, before the
    # handler. No-op when LOOM_POLICY_MODE=off or no rule applies; in
    # shadow it only records; in enforce a denial is a structured 403
    # whose `errors` tell "denied by policy" from "could not evaluate".
    from server.identity.app.policy_gate import PolicyForbiddenError as _PolicyForbidden

    try:
        await gates.policy(op, target, payload, raw_body, request_id=request_id)
    except _PolicyForbidden as e:
        _emit_event(
            op,
            request_id,
            raw_body,
            0,
            success=False,
            error_code="POLICY_FORBIDDEN",
            error=e,
        )
        raise HTTPException(status_code=403, detail=e.to_detail()) from e
    return payload


def _record_failure(
    op: Operation, identity: dict[str, str], elapsed_ms: int, error_code: str
) -> None:
    # Failures must reach the same counter as successes — as mcp.py
    # already does — or `loom.requests{success="false"}` is permanently
    # empty for HTTP and every error-rate panel reads clean while the
    # dashboard's users see 500s. Same attribute set as the success
    # tick in _finish_success, so the two series divide.
    from server.observability import otel as _otel

    try:
        _otel.record_op(
            op.name,
            namespace=identity["namespace"],
            success=False,
            duration_ms=elapsed_ms,
            error_code=error_code,
        )
    except Exception:  # noqa: BLE001 — telemetry never changes a response
        pass


async def _start_telemetry(op: Operation, response: Response, call: _HttpCall) -> None:
    """The OTel span's Loom attributes (with the namespace's capture consent)
    and the X-Trace-Id header."""
    request_id, auth, identity = call.request_id, call.auth, call.identity
    # Tag the OTel span with Loom-specific attributes for tenant/op
    # filtering; no-op when OTel is disabled. Prefer the payload's
    # user_id, but fall back to auth.subject (SENTINEL means unset) so
    # federated tokens still tag traces with the real user.
    effective_user_id = identity.get("user_id")
    if (not effective_user_id or effective_user_id == SENTINEL) and auth is not None:
        effective_user_id = auth.subject
    from server.observability import otel as _otel

    # The payload session_id, not the tenant triple. SENTINEL ("-") means the
    # caller sent none, and must not become a literal session named "-".
    span_session_id = identity.get("session_id")
    if not span_session_id or span_session_id == SENTINEL:
        span_session_id = None

    # Per-namespace consent for capturing prompt/completion text. Resolved
    # here, once, from a 60s cache — never inside the provider call, which
    # would put a ClickHouse round trip on every generation.
    try:
        from server.observability import capture_consent as _consent

        _capture_level = await _consent.resolve(identity["org"], identity["namespace"])
    except Exception:  # noqa: BLE001 — fail closed, not open
        _capture_level = "off"

    _otel.set_loom_attributes(
        op_name=op.name,
        org=identity["org"],
        namespace=identity["namespace"],
        agent=identity.get("agent", "-"),
        user_id=effective_user_id,
        request_id=request_id,
        session_id=span_session_id,
        capture_level=_capture_level,
    )

    # Hand the caller its trace id. It is the same 32-char hex Langfuse uses
    # as its `traceId` and ClickStack stores as `TraceId`, so a client — a
    # bench harness, a support engineer, an SDK — can go straight from "this
    # call" to "this trace" instead of searching by timestamp and hoping.
    trace_id = current_trace_id()  # never raises
    if trace_id:
        response.headers["X-Trace-Id"] = trace_id


async def _run_handler(op: Operation, payload: Any, call: _HttpCall) -> tuple[Any, int]:
    """The handler call. A failure leaves its ledger row and the failure tick,
    and answers with its status: the handler's own
    HTTPException as raised, a LoomServerError as its code, anything else a
    500 INTERNAL_ERROR. Returns the result and the elapsed milliseconds."""
    request_id, raw_body, identity = call.request_id, call.raw_body, call.identity
    start = time.perf_counter()
    try:
        with gates.tenant_slot(call.auth, call.target), llm_free.serving(op):
            result = await op.handler(payload)
    except HTTPException as he:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        _emit_event(
            op,
            request_id,
            raw_body,
            elapsed_ms,
            success=False,
            error_code="HANDLER_HTTP_ERROR",
            # The row names the status and the detail's code, so a refused
            # request is not a mute row.
            error=_http_exception_note(he),
        )
        code = he.detail.get("code") if isinstance(he.detail, dict) else None
        _record_failure(op, identity, elapsed_ms, str(code or "HANDLER_HTTP_ERROR"))
        detail = handler_failure_detail(he.detail, he.status_code, op.idempotent)
        raise HTTPException(he.status_code, detail=detail, headers=he.headers) from he
    except LoomServerError as le:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        _emit_event(
            op,
            request_id,
            raw_body,
            elapsed_ms,
            success=False,
            error_code=le.code,
            # A LoomError row names its message too, so VALIDATION_FAILED /
            # HANDLER_HTTP_ERROR rows say what failed.
            error=le,
        )
        _record_failure(op, identity, elapsed_ms, le.code)
        detail = handler_failure_detail(le.to_detail(), le.status_code, op.idempotent)
        raise HTTPException(status_code=le.status_code, detail=detail, headers=le.headers()) from le
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        # Full traceback at ERROR — the response carries only the request
        # id, so without this a handler bug is undiagnosable from the logs.
        log.exception(
            "op.internal_error %s ns=%s request_id=%s: %s",
            op.name,
            identity["namespace"],
            request_id,
            e,
        )
        _emit_event(
            op,
            request_id,
            raw_body,
            elapsed_ms,
            success=False,
            error_code="INTERNAL_ERROR",
            error=e,
        )
        _record_failure(op, identity, elapsed_ms, "INTERNAL_ERROR")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                # Never str(e): it carries ClickHouse / SQL text. The log above has it.
                "message": f"internal error; request_id={request_id}",
                "request_id": request_id,
            },
        ) from e
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return result, elapsed_ms


async def _finish_success(
    op: Operation,
    call: _HttpCall,
    payload: Any,
    result: Any,
    elapsed_ms: int,
    idem: tuple[idempotency.Scope | None, str | None],
) -> Any:
    """After a successful handler: the ledger row, the success tick, the
    retrieval-quality metrics, the op.ok log line, the response body (with
    its ignored_args) and the post-handler rows. Returns
    the response body."""
    request_id, raw_body, auth, identity = call.request_id, call.raw_body, call.auth, call.identity
    from server.observability import metrics as _metrics
    from server.observability import otel as _otel

    _emit_event(
        op,
        request_id,
        raw_body,
        elapsed_ms,
        success=True,
        error_code="",
        result=result,
    )
    _otel.record_op(
        op.name,
        namespace=identity["namespace"],
        success=True,
        duration_ms=elapsed_ms,
    )
    # Retrieval-quality metrics. Duck-typed on the response, so every
    # search-shaped op is covered without a per-op branch here; a no-op for
    # everything else. Latency and error rate say nothing about whether
    # retrieval actually worked — this is what does. Inside the read's mark: an
    # LLM-free read queues no judge.
    with llm_free.serving(op):
        _metrics.record_search_quality(
            op.name,
            namespace=identity["namespace"],
            result=result,
            org=identity["org"],
            # The judge needs the query; nothing else here does.
            query=str(getattr(payload, "query", "") or ""),
        )
    # One INFO log per successful op, carrying the span's trace_id so the
    # log source correlates each line back to its trace.
    log.info(
        "op.ok %s ns=%s elapsed_ms=%d",
        op.name,
        identity["namespace"],
        elapsed_ms,
        extra={
            "loom.operation": op.name,
            "loom.org": identity["org"],
            "loom.namespace": identity["namespace"],
            "loom.elapsed_ms": elapsed_ms,
            "loom.request_id": request_id,
        },
    )

    response_body = arg_response_body(op, raw_body, result)  # + ignored_args

    # The budget-violation, audit and replay rows, in one concurrent round.
    await post_handler_rows(
        op,
        transport="http",
        request_id=request_id,
        org=identity["org"],
        namespace=identity["namespace"],
        agent=identity["agent"],
        actor=auth.subject if auth is not None else ANONYMOUS_ACTOR,
        input_obj=payload,
        elapsed_ms=elapsed_ms,
        response_body=response_body,
        idem=idem,
        replay_headers={"X-Schema-Version": SCHEMA_VERSION},
    )
    return response_body


def _make_endpoint(op: Operation) -> Callable[[Request, Response], Awaitable[Any]]:
    async def endpoint(request: Request, response: Response) -> Any:
        # The phases run in this order and each refusal raises its own HTTP
        # error: token, body, validation, authorization, policy, rate limit,
        # idempotency replay, then the handler. Nothing after a gate runs
        # when it refuses; a replay is served only to an admitted caller.
        call = _HttpCall(request_id=_begin_request(op, request, response))
        request_id = call.request_id

        # The token first. A request the deployment would refuse as
        # anonymous 401s before its body is read: it leaves no ledger or
        # code-trace row (the body and validation refusals write both),
        # learns no field errors, and never touches a tenant's rate-limit
        # bucket by naming its namespace.
        call.auth = auth = _extract_auth(request)
        # Bound before the body is read, as MCP binds it before validation:
        # a refusal's ledger row reads the token's org and pins from it.
        from server.auth.context import set_current_auth

        set_current_auth(auth)
        bind_claimed_class(auth)
        call.raw_body = raw_body = await _read_body(op, request, request_id, auth)

        payload = _validate_payload(
            op, request_id, gates.with_resolved_org(op, raw_body, auth), auth
        )
        payload, raw_body = _resolve_personal(op, call, payload, raw_body, auth)
        call.raw_body = raw_body

        # The token is verified: its `consumer` claim, else the body's field, else its class.
        bind_current_consumer(raw_body, auth)
        call.identity = request_identity(raw_body)

        # The shared admission sequence (server/transport/gates.py):
        # authorization target, authorization, read scope, policy, then the limiter.
        call.target = target = await _authorize(op, response, call, payload)
        payload = gates.with_target(payload, target)
        payload = await _bind_context_and_policy(op, request, call, target, payload)
        _rate_limit(op, response, call, target)
        replay = await _idempotency_gate(op, request, response, call, target, payload)
        if replay.hit is not None:
            return replay.hit.body
        await _start_telemetry(op, response, call)

        result, elapsed_ms = await _run_handler(op, payload, call)
        _success_headers(response, elapsed_ms, op, raw_body, result)
        return await _finish_success(
            op, call, payload, result, elapsed_ms, (replay.scope, replay.body_hash)
        )

    return endpoint


def _success_headers(
    response: Response, elapsed_ms: int, op: Operation, raw_body: dict[str, Any], result: Any
) -> None:
    """Headers a successful answer carries: the timing pair, the mentor's
    hint when one fires for this call, and the ignored-args header."""
    _stamp_timing(response, elapsed_ms)
    _mentor_headers(op, raw_body, result, response)
    ignored = arg_header_value(op, raw_body, result)
    if ignored:
        response.headers["X-Loom-Ignored-Args"] = ignored


def _mentor_headers(
    op: Operation, raw_body: dict[str, Any], result: Any, response: Response
) -> None:
    """The mentor's one hint for this call as `X-Loom-Hint` /
    `X-Loom-Hint-Code` (the body is unchanged; the SDKs log the header), and
    the code on the request context so the ledger row carries
    `out_hint_code`. Never raises, never reads the store."""
    try:
        from server.insights.app import mentor as _mentor

        # The tenant the call was admitted in, as its ledger row names it.
        tenant = ledger_identity(raw_body)
        hint = _mentor.call_hint(
            op.name,
            raw_body,
            result,
            session_key=_mentor.session_key_for("", {**raw_body, **tenant}),
            org=present(tenant["org"]) or "",
            namespace=present(tenant["namespace"]) or "",
            agent=str(raw_body.get("agent") or "-"),
        )
        set_hint_context(hint[0] if hint else "")
        if hint is not None:
            response.headers["X-Loom-Hint-Code"] = hint[0]
            response.headers["X-Loom-Hint"] = hint[1].encode("ascii", "replace").decode("ascii")
    except Exception:  # noqa: BLE001 — advice never fails a request
        pass


def _http_exception_note(he: HTTPException) -> HTTPException:
    """The ledger's copy of a handler's HTTPException: same class and
    status, the detail reduced to its `code` (or its first 80 characters) so
    `bounded_error` keeps a readable `403: AUTH_INSUFFICIENT` instead of a
    redacted dict."""
    detail = he.detail
    code = detail.get("code") if isinstance(detail, dict) else None
    text = str(code) if code else " ".join(str(detail or "").split())[:80]
    note = HTTPException(status_code=he.status_code, detail=text)
    note.__cause__ = he  # status_class reads an unverified refusal through the chain
    return note


def _emit_event(
    op: Operation,
    request_id: str,
    raw_body: dict[str, Any],
    latency_ms: int,
    *,
    success: bool,
    error_code: str,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    identity = ledger_identity(raw_body)
    # memory_key (memory.* family) lets cost.summary roll up per key; empty
    # for ops without one.
    raw_memory_key = raw_body.get("memory_key", "")
    from server.auth.context import current_auth as _current_auth

    auth = _current_auth()
    impersonated_by = auth.impersonated_by or "" if auth is not None else ""
    emit_agent_event(
        op_name=op.name,
        identity=identity,
        request_id=request_id,
        latency_ms=latency_ms,
        success=success,
        error_code=error_code,
        memory_key=str(raw_memory_key) if raw_memory_key else "",
        impersonated_by=impersonated_by,
        raw_args=raw_body,
        result=result,
        error=error,
        server_fault=answered_unverified(),
        **sli_columns(op.name, "http"),
    )


def attach(app: FastAPI) -> None:
    components: dict[str, Any] = {}
    app.include_router(build_router(components))
    app.middleware("http")(me_route_measured)
    # Outermost: what a request deferred is committed before its answer starts.
    from server.core.request_tail import RequestTailMiddleware
    from server.identity.app.policy_witness import hold_event
    from server.observability.ledger import set_event_hold

    app.add_middleware(RequestTailMiddleware)
    set_event_hold(hold_event)
    base_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = base_openapi()
            defined = schema.setdefault("components", {}).setdefault("schemas", {})
            for name, sub in components.items():
                if defined.setdefault(name, sub) != sub:
                    raise RuntimeError(f"op model {name!r} collides with a route model")
        return app.openapi_schema or base_openapi()

    app.openapi = openapi  # type: ignore[method-assign]


def me_operation_name(path: str) -> str:
    """`/v1/me/service_accounts.create` → `me.service_accounts.create`,
    `/v1/me/set-default-namespace` → `me.set-default-namespace`."""
    tail = path[len("/v1/me/") :].strip("/").replace("/", ".")
    return f"me.{tail}" if tail else "me"


async def me_route_measured(request: Request, call_next: Any) -> Response:
    """A `/v1/me/*` route is an operation. It gets the
    request id, the query tag, the work bag, `Server-Timing` (`app`, `ch`
    with statements and bytes) and one ledger row (`operation = me.<route>`,
    `kind = request`, the verified subject the handler recorded on
    `request.state.loom_caller`), the same as every registered operation.
    Other paths pass straight through."""
    if not request.url.path.startswith("/v1/me/"):
        passthrough: Response = await call_next(request)
        return passthrough
    from server.transport._common import begin_request

    op_name = me_operation_name(request.url.path)
    request_id = request_id_from(request.headers.get("X-Request-Id"))
    bind_request(op_name, request_id)
    begin_request()
    set_current_consumer("")
    set_current_traffic_class("")
    start = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    response.headers["X-Request-Id"] = request_id
    _stamp_timing(response, elapsed_ms)
    subject, org = getattr(request.state, "loom_caller", ("", "")) or ("", "")
    try:
        emit_agent_event(
            op_name=op_name,
            identity={
                "org": org or "-",
                "namespace": "-",
                "agent": "-",
                "user_id": subject or "-",
                "session_id": "-",
            },
            request_id=request_id,
            latency_ms=elapsed_ms,
            success=response.status_code < 400,
            error_code="" if response.status_code < 400 else f"HTTP_{response.status_code}",
            subject=subject or None,
        )
    except Exception as e:  # noqa: BLE001 — telemetry never fails a caller-API answer
        log.debug("me route %s: ledger row not written: %s", op_name, e)
    return response
