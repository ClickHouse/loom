"""The outcome of an MCP `tools/call` once the handler ran: OTel and
retrieval-quality telemetry, the success response and its
post-handler rows, the failure answer, and the mentor's hint.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from server.core import llm_free
from server.transport._common import ledger_identity
from server.transport.gates import present
from server.transport.lean_results import mcp_op_result
from server.transport.mcp_config import TOOL_EXEC_FAILED, _err, _ok
from server.transport.mcp_identity import default_namespace_note
from server.transport.mcp_tools import _emit_mcp_event

if TYPE_CHECKING:
    from server.core.idempotency import Scope
    from server.transport.mcp_call import _AdmittedCall

log = logging.getLogger("server.transport.mcp")


def _record_success_telemetry(call: _AdmittedCall, result: Any, elapsed_ms: int) -> None:
    """Phase 8 of `_call_tool` (success path): the OTel op tick, span scope
    and retrieval-quality metrics. Never raises — telemetry must not break a
    tool call."""
    op, input_obj, auth, request_id = call.op, call.input_obj, call.auth, call.request_id
    mcp_session_id, rl_ns, rl_org = call.mcp_session_id, call.rl_ns, call.rl_org
    # OTel metrics + retrieval quality, at parity with the HTTP transport.
    # This path is how agents reach Loom through MCP clients, and it calls
    # `op.handler` directly rather than
    # going through `transport/http.py`, so without this block every `loom.*`
    # metric and Langfuse trace would describe only the dashboard's HTTP
    # traffic. Emitting the same signals here makes a namespace's retrieval
    # health mean the same thing regardless of how it was called.
    try:
        from server.observability import metrics as _metrics
        from server.observability import otel as _otel

        _otel.record_op(
            op.name,
            namespace=rl_ns or "-",
            success=True,
            duration_ms=elapsed_ms,
            transport="mcp",
        )
        # Scope the span before anything stamps loom.search.* onto it, so MCP
        # search spans carry loom.namespace and loom.operation and the
        # documented click-through (namespace + abstain) finds them.
        _otel.set_loom_attributes(
            **_span_identity(
                op, input_obj, auth, request_id, mcp_session_id, org=rl_org, namespace=rl_ns
            )
        )
        with llm_free.serving(op):  # an LLM-free read queues no judge
            _metrics.record_search_quality(
                op.name,
                namespace=rl_ns or "-",
                result=result,
                transport="mcp",
                org=rl_org or "",
                query=str(getattr(input_obj, "query", "") or ""),
            )
    except Exception as e:  # noqa: BLE001 — telemetry must never break a tool call
        log.debug("mcp otel emit failed for %s: %s", op.name, e)


# JS-safe integer coercion. The wire crosses through V8 clients, which
# silently truncate ints > 2^53 in JSON.parse — version IDs (time_ns-based)
# then mismatch the outputSchema and render as "Tool execution failed".
# Coerce to strings; only fires inside the MCP transport, so SDK / HTTP
# clients still get typed ints.
_JS_SAFE_INT_MAX = (1 << 53) - 1


def _coerce_bigints(o: Any) -> Any:
    if isinstance(o, int) and not isinstance(o, bool) and abs(o) > _JS_SAFE_INT_MAX:
        return str(o)
    if isinstance(o, dict):
        return {k: _coerce_bigints(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_coerce_bigints(v) for v in o]
    return o


def _search_answer(call: _AdmittedCall, result: Any) -> dict[str, Any]:
    """The `loom_search` tool's result: a text summary and the lean sections,
    rendered from the payload as the handler assembled it (its timestamps
    unconverted), for the namespace the call addressed."""
    from server.transport.mcp_tools import search_answer

    answer = search_answer(result.model_dump(), call.arguments, call.rl_ns, call.mcp_session_id)
    answer["structuredContent"] = _coerce_bigints(answer["structuredContent"])
    return answer


async def _success_response(
    call: _AdmittedCall,
    result: Any,
    elapsed_ms: int,
    idem: tuple[Scope | None, str | None],
    mentor_hint: tuple[str, str] | None,
) -> dict[str, Any]:
    """Phase 9 of `_call_tool` (success path): the wire-safe full result,
    the post-handler rows (budget, audit, idempotency — they keep the full
    shape), then the lean result the client reads."""
    req_id, op, input_obj, arguments = call.req_id, call.op, call.input_obj, call.arguments
    auth, request_id, rl_ns, rl_org = call.auth, call.request_id, call.rl_ns, call.rl_org
    result_dict = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    result_dict = _coerce_bigints(result_dict)

    # Budget violation, audit and idempotency rows in one concurrent round,
    # not three awaited writes in a row.
    from server.transport import post_rows
    from server.transport._common import ANONYMOUS_ACTOR

    await post_rows.post_handler_rows(
        op,
        transport="mcp",
        request_id=request_id,
        # The tenant the gate authorized, as HTTP records it: the input's,
        # else the plane default a code.* / tracker.* op was admitted under.
        org=rl_org or "-",
        namespace=rl_ns or "-",
        agent=str(getattr(input_obj, "agent", "-") or "-"),
        actor=(auth.subject if auth is not None else ANONYMOUS_ACTOR),
        input_obj=input_obj,
        elapsed_ms=elapsed_ms,
        response_body=result_dict,
        idem=idem,
        replay_headers={"X-Loom-Transport": "mcp"},
    )

    if op.name == "search.query":
        return _ok(req_id, _search_answer(call, result))

    # content[0].text is raw JSON (no prose prefix) — some clients parse it
    # for display and a prefix breaks the parse. structuredContent mirrors it
    # for clients that prefer typed access. Both carry the
    # lean shape (hits and content, not ranking) unless the call said
    # explain=true; the ledger and idempotency store above keep the full one.
    lean_dict = mcp_op_result(op.name, result_dict, arguments, mentor_hint)
    from server.transport.namespace_resolve import RESOLVED

    resolved = RESOLVED.get()
    if resolved and isinstance(lean_dict, dict):
        lean_dict = {**lean_dict, "namespace_resolved": resolved}
    if isinstance(lean_dict, dict):  # a non-home default says so
        lean_dict = {**lean_dict, **default_namespace_note()}
    return _ok(
        req_id,
        {
            "content": [{"type": "text", "text": json.dumps(lean_dict)}],
            "isError": False,
            "structuredContent": lean_dict,
        },
    )


def _handler_failed(
    e: BaseException,
    req_id: Any,
    name: str,
    op: Any,
    arguments: dict[str, Any],
    input_obj: Any,
    auth: Any,
    request_id: str,
    mcp_session_id: str,
    *,
    rl_ns: str | None,
    rl_org: str = "",
    elapsed_ms: int,
) -> dict[str, Any]:
    """The handler raised. A 4xx refusal (LOCKED, PINNED,
    a task state, BLACKLISTED, NOT_FOUND, 429 — not 401) is an `isError`
    result carrying its structured body, recorded under its own code. A 5xx
    LoomServerError (OVERLOADED, BACKEND_TIMEOUT, SCHEMA_PENDING, EMBED_TIMEOUT)
    stays a JSON-RPC error that carries HTTP's body and is recorded under its
    code; any other exception is one that says only "internal error" and is
    logged with its traceback."""
    from server.transport.mcp_refusals import refusal_body, refusal_text, server_failure

    refused = refusal_body(e)
    failure = server_failure(e) if refused is None else None
    if refused is not None:
        log.info("mcp.refused %s request_id=%s: %s", op.name, request_id, refused.get("code"))
    elif failure is not None:  # the raiser logged its cause; no traceback for a named 5xx
        log.warning("mcp.unavailable %s request_id=%s: %s", op.name, request_id, failure["code"])
    else:
        # Full traceback at ERROR — the JSON-RPC error carries only the
        # request id, so this line is the only place the cause is visible.
        log.exception("mcp.internal_error %s request_id=%s: %s", op.name, request_id, e)
    _emit_mcp_event(
        op.name,
        arguments,
        elapsed_ms,
        success=False,
        error_code=str((refused or failure or {}).get("code") or "INTERNAL_ERROR"),
        request_id=request_id,
        error=e,
    )
    # Failures must reach the same counter as successes, or the
    # `success=false` series is permanently empty for MCP and every error
    # dashboard reads clean while agents are failing.
    try:
        from server.observability import otel as _otel

        _otel.record_op(
            op.name, namespace=rl_ns or "-", success=False, duration_ms=elapsed_ms, transport="mcp"
        )
        # Scope the FAILING span too, so MCP errors filter by tenant.
        _otel.set_loom_attributes(
            **_span_identity(
                op, input_obj, auth, request_id, mcp_session_id, org=rl_org, namespace=rl_ns or ""
            )
        )
    except Exception:  # noqa: BLE001 — telemetry must never mask the handler's error
        pass
    if refused is not None:
        return _ok(
            req_id,
            {
                "content": [{"type": "text", "text": refusal_text(refused)}],
                "isError": True,
                "structuredContent": refused,
            },
        )
    if failure is not None:  # HTTP's 503/504 body, as the error's message and data
        return _err(
            req_id,
            TOOL_EXEC_FAILED,
            f"tool execution failed: {refusal_text(failure)}; request_id={request_id}",
            data={**failure, "tool": name, "request_id": request_id},
        )
    # The exception's text (ClickHouse / SQL) stays in the log line above.
    return _err(
        req_id,
        TOOL_EXEC_FAILED,
        f"tool execution failed: internal error; request_id={request_id}",
        data={"tool": name, "request_id": request_id},
    )


def _emit_success(
    op: Any,
    arguments: dict[str, Any],
    elapsed_ms: int,
    request_id: str,
    result: Any,
    mcp_session_id: str,
) -> tuple[str, str] | None:
    """The success row of a tool call, with the mentor's hint computed first
    so the row carries `out_hint_code`. Returns the hint for the result."""
    hint = _mentor_hint(op.name, arguments, result, mcp_session_id)
    _emit_mcp_event(
        op.name,
        arguments,
        elapsed_ms,
        success=True,
        error_code="",
        request_id=request_id,
        result=result,
    )
    return hint


def _mentor_hint(
    op_name: str, arguments: dict[str, Any], result: Any, mcp_session_id: str
) -> tuple[str, str] | None:
    """The mentor's one hint for this call, or None; the
    code is published on the request context for the ledger row. Never
    raises and never reads the store."""
    try:
        from server.insights.app import mentor as _mentor
        from server.observability.ledger import set_hint_context

        # The tenant the call was admitted in, as its ledger row names it.
        tenant = ledger_identity(arguments)
        hint = _mentor.call_hint(
            op_name,
            arguments,
            result,
            session_key=_mentor.session_key_for(mcp_session_id, {**arguments, **tenant}),
            org=present(tenant["org"]) or "",
            namespace=present(tenant["namespace"]) or "",
            agent=str(arguments.get("agent") or "-"),
        )
        set_hint_context(hint[0] if hint else "")
        return hint
    except Exception:  # noqa: BLE001 — advice never fails a tool call
        return None


def _span_identity(
    op: Any,
    input_obj: Any,
    auth: Any,
    request_id: Any,
    mcp_session_id: str = "",
    *,
    org: str = "",
    namespace: str = "",
) -> dict[str, Any]:
    """Identity kwargs for `otel.set_loom_attributes` on the MCP path.

    `org` / `namespace` are the tenant the gate authorized (the input's, else
    a code.* / tracker.* op's plane default), as HTTP's span and capture
    consent read them; the input's own fields only when none is given.
    Mirrors transport/http.py, including its fallback to `auth.subject` when the
    payload user_id is empty or the SENTINEL "-" — that fallback exists so
    federated tokens still tag traces with the real user; without it every MCP
    request from a federated-JWT agent would carry no `enduser.id`, and
    per-user trace filtering would cover HTTP traffic only.
    """
    uid = str(getattr(input_obj, "user_id", "") or "")
    if (not uid or uid == "-") and auth is not None:
        uid = str(getattr(auth, "subject", "") or "")
    # The conversation session the caller sent. "-" is the unset sentinel and must
    # not become a literal session named "-".
    # Payload session first, transport session as the fallback. The payload
    # `session_id` is the CONVERSATION — a loom concept that scopes memory — and
    # is what an operator means by "this conversation". `Mcp-Session-Id` is the
    # transport connection, which is still far better than nothing: it groups one
    # client's tool calls together.
    sid = str(getattr(input_obj, "session_id", "") or "")
    if sid == "-":
        sid = ""
    if not sid:
        sid = str(mcp_session_id or "")
    return {
        "op_name": getattr(op, "name", "") or "",
        "org": str(org or getattr(input_obj, "org", "") or "-"),
        "namespace": str(namespace or getattr(input_obj, "namespace", "") or "-"),
        "agent": str(getattr(input_obj, "agent", "") or "-"),
        "user_id": uid or None,
        "request_id": str(request_id or ""),
        "session_id": sid or None,
    }
