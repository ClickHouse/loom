"""MCP `tools/call`, up to and around the handler: resolve the tool, bind
the caller's identity and the session run, validate, authorize, admit
(traffic class, policy, rate limit) and replay idempotent calls, then run
the handler. The outcome (telemetry, the result, a failure) is
``mcp_outcome``'s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from server.code.app.common import schedule_code_trace_validation_failure
from server.code.domain.scope import apply_code_scope, current_code_scope
from server.core import llm_free
from server.core.errors import LoomServerError
from server.core.naming import named
from server.identity.app.caller_home import home_namespace
from server.observability.consumer import ASSISTANT_CONSUMER
from server.operations.base import Operation
from server.operations.toolsets import (
    _MCP_HIDDEN_OPS,
    ASSISTANT_FORCED_ARGS,
    ASSISTANT_NAMED_AGENT_OPS,
    MCP_ASSISTANT_OPS,
)
from server.transport import gates
from server.transport import mcp_runs as _mcp_runs
from server.transport._common import begin_call, bind_request, validation_note
from server.transport._common import validation_errors as _validation_errors
from server.transport.arg_feedback import feedback, refused_result, tool_error
from server.transport.assistant_call import assistant_call, set_assistant_call
from server.transport.lean_results import lean_result
from server.transport.mcp_config import (
    _LOOM_ACTIVATE_TOOL_NAME,
    INVALID_PARAMS,
    _err,
    _ok,
)
from server.transport.mcp_defaults import coerce_search_arguments, mcp_default_arguments
from server.transport.mcp_identity import _caller_defaults, note_default_namespace
from server.transport.mcp_ledger import batch_allows_row, emit_token_refusal
from server.transport.mcp_op import UNKNOWN_OP
from server.transport.mcp_outcome import (
    _emit_success,
    _handler_failed,
    _record_success_telemetry,
    _span_identity,
    _success_response,
)
from server.transport.mcp_primer import _render_primer_and_emit
from server.transport.mcp_tools import TOOLS_BY_NAME, _emit_mcp_event, assistant_connection

if TYPE_CHECKING:
    from server.core.idempotency import Scope
    from server.core.ratelimit import CheckResult

log = logging.getLogger("server.transport.mcp")


async def _ratelimit_retry_once(
    rl: CheckResult, op: Operation, auth: Any, target: gates.Target, *, traffic_class: str
) -> CheckResult | None:
    """One bounded wait for the limiter's own `retry_after_s`, then one
    re-check through the same gate as the first check (so the same buckets:
    a `bench` call re-checks the bench bucket, never production's). Returns
    the second answer, or the first unchanged when the hint exceeds
    `mcp_ratelimit_retry_max_wait_s` (0 never waits) — a long wait is the
    client's call, not a hidden stall inside a tool call. A limiter that
    fails on the re-check admits the call, as it does on the first."""
    from server.settings import settings as _settings

    wait = float(rl.retry_after_s or 0.0)
    if wait <= 0 or wait > float(_settings.mcp_ratelimit_retry_max_wait_s):
        return rl
    await asyncio.sleep(wait)
    return gates.rate_limit(op, auth, target, traffic_class)


MCP_TASK_LIST_FIELDS: tuple[str, ...] = (
    "task_id",
    "title",
    "status",
    "kind",
    "priority",
    "from_agent",
    "owner",
    "outcome",
    "created_at",
)


def _scope_arguments(
    op_name: str, arguments: dict[str, Any], defaults: dict[str, Any] | None, explicit_agent: bool
) -> None:
    """The two identity adjustments after the defaults fill. memory.update
    resolves its key namespace-wide unless `agent` narrows it; the client
    slug filled in above would narrow every update to the client's own rows,
    so a key a read returned from another writer would answer not_found —
    only an explicit agent narrows.
    The connection may name the checkout's code namespace
    (X-Loom-Code-Scope, from the plugin's headers helper); a code.* call that
    names none is scoped to it, explicit values win, authz runs on it."""
    if defaults and op_name == "memory.update" and not explicit_agent:
        arguments["agent"] = ""
    # An MCP context.pack is a mid-session call; the agent's
    # `session:<project>` bookmarks are the SessionStart primer's resume notes
    # for whichever repository wrote them. Unnamed, none is served ("-").
    if op_name == "context.pack" and not arguments.get("session_block"):
        arguments["session_block"] = "-"
    # An MCP task.list reads like a person scanning the inbox
    # — the lean row, newest first — not the full payload in queue order (a
    # piled-up test inbox cost ~2.8K tokens and hid the task just posted).
    # task.get has the whole row; explicit fields / newest_first win.
    if op_name == "task.list":
        if not arguments.get("fields"):
            arguments["fields"] = list(MCP_TASK_LIST_FIELDS)
        arguments.setdefault("newest_first", True)
    if op_name == SEARCH_OP:
        coerce_search_arguments(arguments)
    apply_code_scope(op_name, arguments, current_code_scope())


# The op MCP serves as the `loom_search` tool.
SEARCH_OP = "search.query"


async def _address_home(arguments: dict[str, Any], auth: Any) -> None:
    """An unaddressed `all_mine` search addresses the caller's own namespace
    first, even when the token's first claimed namespace is a teammate's (the
    page park, the exposures and the ledger row follow it): the namespace the
    defaults filled in moves to the caller's home among the fan-out set. The
    set is memory-kind only, so a caller whose saved default is a code
    namespace lands on its first memory namespace. An explicit namespace is
    never rewritten (the gate refuses it if it must)."""
    if arguments.get("namespace_scope") != "all_mine":
        return
    from server.auth.fanout import resolve_fanout

    try:
        fanout = await resolve_fanout(str(arguments.get("org") or ""))
    except Exception as e:  # noqa: BLE001 — no fan-out namespaces narrows the search, never widens
        log.warning("loom_search namespace resolution failed (non-fatal): %s", e)
        return
    # The home is chosen from every namespace of the caller, before the cap.
    accessible = list(fanout.namespaces)
    if accessible:
        subject = str(getattr(auth, "subject", "") or "") if auth is not None else ""
        arguments["namespace"] = home_namespace(
            accessible, subject, str(arguments.get("namespace") or "")
        )


def _address_first_claim(arguments: dict[str, Any], readable: list[str]) -> bool:
    """A `loom_search` naming no namespace from a token with no default one
    searches the token's first readable claim (`readable`, from its
    defaults); the gate still checks it. True when it filled one in."""
    if readable:
        arguments["namespace"] = readable[0]
    return bool(readable)


# History and related name a KEY — a key another client
# wrote answered `{}` / source_not_found under the caller's own slug.
_MCP_CROSS_AGENT_READS: frozenset[str] = frozenset(
    {"memory.search", "memory.list", "memory.search_trace", "memory.history", "memory.related"}
)


def _forbidden_result(
    op: Any, err: Exception, auth: Any, org: str | None, ns: str | None
) -> dict[str, Any]:
    """A role-check refusal. On a code/tracker index it says how to get in
    and that retrying elsewhere will not help."""
    from server.transport import access_refusal as ar

    if ar.is_plane_op(op.name) and ns:
        return ar.mcp_result(ar.refusal(str(getattr(auth, "subject", "") or ""), org or "", ns))
    msg = f"role check failed for {op.name}: {err}"
    body = {"error": msg, "code": gates.ROLE_REFUSAL_CODE, "required_role": op.required_role.name}
    return {"content": [{"type": "text", "text": msg}], "isError": True, "structuredContent": body}


async def _call_tool(
    req_id: Any,
    params: dict[str, Any],
    auth: Any = None,
    client_name: str | None = None,
    mcp_session_id: str = "",
    toolset: str | None = None,
) -> dict[str, Any]:
    """One MCP `tools/call`, in phases (each below this function, in order):
    resolve the tool → bind the caller's identity → stamp the session run's
    id → argument refusal + validation → authorization → admission (traffic
    class, policy, rate limit) → the session run begins → idempotency replay
    (only for an admitted caller) → open the trace → span identity
    (inline) → the handler → success telemetry → the response. Every phase
    that can end the call returns its response and this function returns it
    at once, so no gate runs after a refusal and none runs after the
    handler."""
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if not name or not isinstance(name, str):
        return _err(req_id, INVALID_PARAMS, "tools/call requires 'name' (string)")
    if not isinstance(arguments, dict):
        return _err(req_id, INVALID_PARAMS, "tools/call 'arguments' must be an object")

    begin_call()

    tool = await _resolve_connection_tool(req_id, name, arguments, auth, client_name, toolset)
    if isinstance(tool, dict):
        return tool  # loom_activate's answer, or the not-available refusal
    op = tool

    request_id, defaults = await _bind_caller_identity(op, arguments, auth, client_name)

    run_caller, stamped_run = _stamp_session_run(op, arguments, defaults, mcp_session_id)

    client_idem_key = _pop_idempotency_key(op, arguments)

    arguments.update(gates.with_resolved_org(op, arguments, auth))
    validated = _validated_input(req_id, op, arguments, request_id)
    if isinstance(validated, dict):
        return validated  # misspelled-argument refusal, or VALIDATION_FAILED
    input_obj = validated

    target = await _authorize_call(req_id, op, input_obj, arguments, auth, request_id)
    if isinstance(target, dict):
        return target  # AUTH_INSUFFICIENT — nothing below (limiter, handler) runs
    input_obj = gates.with_target(input_obj, target)

    # The resolved tenant the telemetry and post-handler rows name, as HTTP's
    # identity does after authorization.
    rl_ns = target.register_namespace or ""
    rl_org = target.org or ""

    call = _AdmittedCall(
        req_id,
        op,
        input_obj,
        arguments,
        auth,
        request_id,
        mcp_session_id,
        rl_ns,
        rl_org,
        target,
        stamped_run,
    )
    refused = await _admission_refusal(call)
    if refused is not None:
        return refused  # POLICY_FORBIDDEN, AUTH_INSUFFICIENT (read scope) or RATE_LIMITED
    input_obj = call.input_obj  # names the user a scoped read binds

    await _bind_session_run(op, defaults, mcp_session_id, run_caller)

    # The replay cache is read only for an admitted caller.
    replay, idem_scope, idem_body_hash = await _idempotency_gate(call, name, client_idem_key)
    if replay is not None:
        return replay  # IDEMPOTENT_CONFLICT refusal, or the cached result replayed

    # Scope the span and publish the identity ContextVar BEFORE the handler runs.
    # The two `set_loom_attributes` calls after `op.handler(...)` (in
    # `_handler_failed` / `_record_success_telemetry`) happen once it
    # returns, which is fine for span attributes but useless for the provider
    # spans: here `set_loom_attributes` sets the identity, and every `llm.*` /
    # `embed.*` span of this request is created INSIDE the handler.
    # Set later, MCP generations would carry no loom.org/namespace/operation
    # and no Langfuse session or user. In a JSON-RPC batch the ContextVar is
    # task-scoped, so entry N's provider spans would carry entry N-1's tenant.
    # Setting it here, once per entry before any LLM call, prevents both.
    try:
        from server.observability import capture_consent as _consent
        from server.observability import otel as _otel_pre

        _ident = _span_identity(
            op, input_obj, auth, request_id, mcp_session_id, org=rl_org, namespace=rl_ns
        )
        # Same per-namespace consent resolution as the HTTP path: once per
        # request, from a 60s cache, never inside the provider call.
        try:
            _ident["capture_level"] = await _consent.resolve(
                str(_ident.get("org") or ""), str(_ident.get("namespace") or "")
            )
        except Exception:  # noqa: BLE001 — fail closed
            _ident["capture_level"] = "off"
        _otel_pre.set_loom_attributes(**_ident)
    except Exception:  # noqa: BLE001 — identity stamping must not fail a tool call
        from server.observability import genai as _genai

        # Not the previous batch entry's org: this entry's model calls name no org,
        # so they skip the model caches and refuse.
        _genai.set_identity(operation=op.name, capture_level="off")

    start = time.perf_counter()
    try:
        with gates.tenant_slot(auth, target), llm_free.serving(op):
            result = await op.handler(input_obj)
    except Exception as e:  # noqa: BLE001 — any handler error becomes a refusal or RPC error
        return _handler_failed(
            e,
            req_id,
            name,
            op,
            arguments,
            input_obj,
            auth,
            request_id,
            mcp_session_id,
            rl_ns=rl_ns,
            rl_org=rl_org,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    mentor_hint = _emit_success(op, arguments, elapsed_ms, request_id, result, mcp_session_id)

    _record_success_telemetry(call, result, elapsed_ms)

    return await _success_response(
        call, result, elapsed_ms, (idem_scope, idem_body_hash), mentor_hint
    )


def _stamp_session_run(
    op: Operation, arguments: dict[str, Any], defaults: dict[str, str] | None, mcp_session_id: str
) -> tuple[_mcp_runs.Caller | None, bool]:
    """Phase 3 of `_call_tool`: stamp the session run's id into a write that names
    no run, before validation; the run itself begins only once the call is
    admitted (`_bind_session_run`). Returns the caller the run is filed under
    (None when the op takes no run) and whether `run_id` was stamped."""
    # A read is never stamped: its `run_id` field filters to the run's own rows, so
    # it is only labelled through the ledger ContextVar.
    try:
        from server.auth.context import current_auth

        if not defaults or not _mcp_runs.takes_run(op):
            return None, False
        # An investigation's reads are not the work of a run: an assistant call's session
        # opens none.
        if assistant_call():
            return None, False
        # The run row lives in the caller's org and default namespace, whatever
        # namespace a call names, so every web process files it under one key.
        caller = _mcp_runs.Caller.of_call(current_auth(), arguments)
        run_id = _mcp_runs.run_id_for(mcp_session_id, defaults, caller)
        if run_id and _mcp_runs.stamps_run(op) and not named(str(arguments.get("run_id") or "")):
            arguments["run_id"] = run_id
            return caller, True
        return caller, False
    except Exception as _run_err:  # noqa: BLE001 — the ledger never fails a tool call
        log.debug("mcp session run failed for %s (non-fatal): %s", op.name, _run_err)
        return None, False


async def _bind_session_run(
    op: Operation,
    defaults: dict[str, str] | None,
    mcp_session_id: str,
    caller: _mcp_runs.Caller | None,
) -> None:
    """Begin (or join) the session's implicit run for an admitted call and bind it
    for the ledger, so a refused call never writes a run row."""
    session_run_id = ""
    try:
        from server.observability.run_context import set_ledger_run

        if caller is not None:
            run = await _mcp_runs.ensure(mcp_session_id, defaults, caller)
            if run is not None:
                session_run_id = run.run_id
        set_ledger_run(session_run_id)
    except Exception as _run_err:  # noqa: BLE001 — the ledger never fails a tool call
        log.debug("mcp session run failed for %s (non-fatal): %s", op.name, _run_err)


def _assistant_refusal(req_id: Any, name: str, auth: Any) -> dict[str, Any] | None:
    """The refusal for a tool the assistant toolset does not carry, None for one it does.
    The token may allow more; this connection serves only the toolset's reads."""
    op = TOOLS_BY_NAME.get(name)
    if op is not None and op.name in MCP_ASSISTANT_OPS and op.read_only:
        return None
    if op is not None:
        # A registry op the toolset leaves out is ledgered by its name; an unknown name,
        # chosen by the client, is not written to the ledger at all.
        emit_token_refusal(op.name, auth, error_code=UNKNOWN_OP, consumer=ASSISTANT_CONSUMER)
    msg = f"Tool {name!r} is not available on the assistant toolset."
    body = {"error": msg, "code": UNKNOWN_OP, "tool": name}
    return _ok(
        req_id,
        {"content": [{"type": "text", "text": msg}], "isError": True, "structuredContent": body},
    )


async def _resolve_connection_tool(
    req_id: Any,
    name: str,
    arguments: dict[str, Any],
    auth: Any,
    client_name: str | None,
    toolset: str | None,
) -> Operation | dict[str, Any]:
    """`_resolve_tool`, and on the assistant toolset first its refusal of any tool it does
    not carry, then its forced arguments and consumer stamped over the model's own (in
    place), so the model cannot argue either away. A token whose `consumer` claim names
    another consumer keeps its claim (`resolve_consumer_and_source`)."""
    on_assistant = assistant_connection(toolset)
    set_assistant_call(on_assistant)
    if not on_assistant:
        return await _resolve_tool(req_id, name, auth, client_name, toolset)
    refused = _assistant_refusal(req_id, name, auth)
    if refused is not None:
        return refused
    # The roster carries the plane searches the other listings hide in favour of
    # `loom_search`: an investigation reads one plane at a time.
    tool = await _resolve_tool(req_id, name, auth, client_name, toolset, allow_hidden=True)
    if not isinstance(tool, dict):
        arguments.update(ASSISTANT_FORCED_ARGS.get(tool.name, {}))
        arguments["consumer"] = ASSISTANT_CONSUMER
    return tool


async def _resolve_tool(
    req_id: Any,
    name: str,
    auth: Any,
    client_name: str | None,
    toolset: str | None = None,
    *,
    allow_hidden: bool = False,
) -> Operation | dict[str, Any]:
    """Phase 1 of `_call_tool`: the tool name → its registry op, or the
    whole JSON-RPC response when the call ends here (`loom_activate` ran,
    or the tool is unknown / hidden)."""
    # loom_activate is answered before the registry lookup (it is no op); it
    # still emits observability so its calls aren't invisible.
    if name == _LOOM_ACTIVATE_TOOL_NAME:
        primer = await _render_primer_and_emit(auth, client_name, "loom_activate", toolset)
        return _ok(
            req_id,
            {
                "content": [{"type": "text", "text": primer}],
                "isError": False,
                "structuredContent": {"primer": primer},
            },
        )

    op = TOOLS_BY_NAME.get(name)
    if op is None or (op.name in _MCP_HIDDEN_OPS and not allow_hidden):
        # Return a result-with-isError, not a JSON-RPC error frame: some
        # clients drop conversation state on JSON-RPC errors (surfaces as
        # "Tool result could not be submitted"). Both are spec-legal.
        msg = f"Tool {name!r} is not available."
        # The code loom_op answers for a name no op answers to.
        body: dict[str, Any] = {"error": msg, "code": UNKNOWN_OP, "tool": name}
        if op is not None:  # A hidden op says why and what instead
            from server.transport.mcp_op import hidden_refusal

            body = {**(hidden_refusal(op.name) or {}), "tool": name}
            emit_token_refusal(op.name, auth, error_code="OP_NOT_ON_MCP")
            msg = f"{body.get('error', msg)}; instead: {body.get('instead', '')}"
        return _ok(
            req_id,
            {
                "content": [{"type": "text", "text": msg}],
                "isError": True,
                "structuredContent": body,
            },
        )
    return op


async def _bind_caller_identity(
    op: Operation, arguments: dict[str, Any], auth: Any, client_name: str | None
) -> tuple[str, dict[str, str] | None]:
    """Phase 2 of `_call_tool`: publish the principal and consumer, mint the
    request id, and fill the caller's identity defaults into `arguments`
    (in place). Returns the request id and the defaults (None = no caller
    defaults), both read by later phases."""
    # Publish the verified principal on the per-request ContextVar so
    # current_auth()-reading handlers (list_mine, browse/join/leave, …) see
    # the caller. HTTP does this in _make_endpoint; without it those reject.
    from server.auth.context import set_current_auth
    from server.identity.domain.read_scope import clear_request_read_filters

    set_current_auth(auth)
    clear_request_read_filters()
    # The consumer column — the call's own `consumer`, else
    # the token's subject class — published for the event, audit and budget
    # writers of this call.
    from server.observability.consumer import bind_current_consumer
    from server.observability.traffic_class import bind_claimed_class

    bind_current_consumer(arguments, auth)
    bind_claimed_class(auth)

    request_id = str(uuid.uuid4())
    bind_request(op.name, request_id)

    # Fill in identity-tuple defaults the caller omitted (LLMs call with just
    # the action args). Existing args win — explicit namespace is honored.
    # code.* ops are EXEMPT: the gate authorizes them against the code plane's
    # org and namespace and `gates.with_target` fills those in, whereas the
    # caller's MEMORY namespace here would point every chat-surface code.search
    # at personal-<user> — an empty index, so the tool would always abstain.
    try:
        defaults = await _caller_defaults(auth, client_name)
    except LoomServerError as e:
        # A grants read past its cache's stale window (a 503), before the gate:
        # counted against the server in the token's org (the call's identity
        # is not filled in yet), then answered as raised.
        emit_token_refusal(op.name, auth, error_code=e.code, request_id=request_id)
        raise
    explicit_agent = bool(arguments.get("agent"))
    # A blank or "-" namespace names none, as the gates read it.
    unnamed = not gates.present(arguments.get("namespace"))
    # An empty default namespace (a delegated token whose claim holds no home)
    # is not filled in: the call then fails validation naming `namespace`.
    filled_ns = (
        bool(defaults and defaults["namespace"])
        and "namespace" in op.input_model.model_fields
        and unnamed
        and not op.name.startswith("code.")
    )
    if defaults and not op.name.startswith("code."):
        field_names = set(op.input_model.model_fields.keys())
        for k, v in defaults.items():
            missing = unnamed if k == "namespace" else not arguments.get(k)
            if v and k in field_names and missing:
                arguments[k] = v
    if defaults and defaults["namespace"] and "lesson_namespaces" in op.input_model.model_fields:
        # a code read's lessons include the caller's own memories, from its memory home
        arguments.setdefault("lesson_namespaces", [defaults["namespace"]])
    if op.name == SEARCH_OP and defaults and not defaults["namespace"] and unnamed:
        filled_ns = _address_first_claim(arguments, defaults.get("readable") or [])
    note_default_namespace(filled_ns, str(arguments.get("namespace") or ""), auth)
    _scope_arguments(op.name, arguments, defaults, explicit_agent)
    if op.name == SEARCH_OP and filled_ns:
        await _address_home(arguments, auth)

    # Read-shaped ops default to cross-agent within the namespace (memory is
    # shared across tools). Writes keep the caller's own agent for provenance.
    # Caller can opt back into strict-agent by passing their agent explicitly.
    if defaults and op.name in _MCP_CROSS_AGENT_READS:
        if not arguments.get("agent") or arguments.get("agent") == defaults.get("agent"):
            arguments["agent"] = "*"
    # The assistant writes nothing, so its client's own agent holds no rows. A read it names
    # no agent for keeps the op's own default (`memory.ask` across agents, `mentor.brief`
    # the namespace's top writer); a read of one writer's rows (ASSISTANT_NAMED_AGENT_OPS)
    # is refused for the missing `agent` rather than answering empty. `memory.get` keeps
    # the client's agent and falls back to the row under any agent itself.
    agent_field = op.input_model.model_fields.get("agent")
    if assistant_call() and not explicit_agent and agent_field is not None:
        if op.name in ASSISTANT_NAMED_AGENT_OPS:
            arguments.pop("agent", None)
        elif isinstance(agent_field.default, str) and op.name not in _MCP_CROSS_AGENT_READS:
            arguments["agent"] = agent_field.default
    return request_id, defaults


def _pop_idempotency_key(op: Operation, arguments: dict[str, Any]) -> str:
    """The client's idempotency key for an idempotent op, "" when absent.
    MCP has no Idempotency-Key header, so it rides as a hidden
    `_idempotency_key` / `idempotency_key` argument, popped before Pydantic
    validation so input models stay unchanged. An input model that declares
    `idempotency_key` itself (task.create's durable dedupe) keeps it."""
    if not op.idempotent:
        return ""
    raw_idem = arguments.pop("_idempotency_key", None)
    if raw_idem is None:
        if "idempotency_key" in op.input_model.model_fields:
            raw_idem = arguments.get("idempotency_key")
        else:
            raw_idem = arguments.pop("idempotency_key", None)
    return str(raw_idem) if raw_idem else ""


async def _idempotency_gate(
    call: _AdmittedCall, name: str, client_key: str
) -> tuple[dict[str, Any] | None, Scope | None, str | None]:
    """The replay decision (`gates.idempotency_check`) for the call's key,
    rendered: a response ends the call (IDEMPOTENT_CONFLICT or
    IDEMPOTENT_IN_PROGRESS, or the cached
    result replayed); otherwise the scope and hash ride to the post-handler
    rows. The validated input is hashed as HTTP hashes it; a run the session
    stamped is not part of what the caller asked."""
    req_id, op, arguments, request_id = call.req_id, call.op, call.arguments, call.request_id
    start = time.perf_counter()
    replay = await gates.idempotency_check(
        op,
        call.auth,
        call.target,
        client_key,
        call.input_obj,
        unset=("run_id",) if call.stamped_run else (),
    )
    if replay.conflict:
        code = "IDEMPOTENT_IN_PROGRESS" if replay.in_progress else "IDEMPOTENT_CONFLICT"
        # After the rate limiter, which bounds these rows: one per entry.
        _emit_mcp_event(
            op.name, arguments, 0, success=False, error_code=code, request_id=request_id
        )
        msg = "Idempotency key reused with different tool arguments."
        body: dict[str, Any] = {"error": msg, "code": code}
        if replay.in_progress:
            msg = gates.IN_PROGRESS_MESSAGE
            body = {"error": msg, "code": code, "in_progress": True, "retry_after_s": 1}
        return (
            _ok(
                req_id,
                {
                    "content": [{"type": "text", "text": msg}],
                    "isError": True,
                    "structuredContent": body,
                },
            ),
            replay.scope,
            replay.body_hash,
        )
    if replay.hit is not None:
        cached = lean_result(replay.hit.body, arguments)
        # The lookup that answered it, as a handler's run is timed.
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        _emit_mcp_event(
            op.name, arguments, elapsed_ms, success=True, error_code="", request_id=request_id
        )
        result = {
            "content": [{"type": "text", "text": json.dumps(cached, default=str)}],
            "isError": False,
            "structuredContent": cached if isinstance(cached, dict) else {"result": cached},
            "_meta": {"idempotentReplay": True},
        }
        return _ok(req_id, result), replay.scope, replay.body_hash
    return None, replay.scope, replay.body_hash


def _validated_input(
    req_id: Any, op: Operation, arguments: dict[str, Any], request_id: str
) -> BaseModel | dict[str, Any]:
    """Phase 4 of `_call_tool`: the misspelled-argument refusal, then
    Pydantic validation. Returns the input model, or the refusal response
    (a dict) when the call ends here."""
    from server.transport.arg_check import named_argument_error

    refused = named_argument_error(op, arguments)
    if refused is not None:
        _emit_refused(
            op.name, arguments, error_code=refused.error_detail()["code"], request_id=request_id
        )
        return _ok(req_id, refused_result(op, refused.error_detail()))

    # Validate input via Pydantic
    try:
        input_obj = op.input_model.model_validate(mcp_default_arguments(op.name, arguments))
    except ValidationError as e:
        # The failing field on the row, and a code.* trace row (each once a batch).
        errors = _validation_errors(e)
        if _emit_refused(
            op.name,
            arguments,
            error_code="VALIDATION_FAILED",
            request_id=request_id,
            error=validation_note(errors),
        ):
            schedule_code_trace_validation_failure(op.name, arguments, errors)
        return _ok(req_id, tool_error(*feedback(op, arguments, errors)))
    return input_obj


async def _authorize_call(
    req_id: Any,
    op: Operation,
    input_obj: BaseModel,
    arguments: dict[str, Any],
    auth: Any,
    request_id: str,
) -> gates.Target | dict[str, Any]:
    """Phase 5 of `_call_tool`: the shared authorization target and gate
    (server/transport/gates.py) — role / namespace check, restricted-access
    audit, first-write registration. Returns the target, or the refusal
    response: AUTH_INSUFFICIENT (a code/tracker index says how to get in) or
    IDENTITY_INCOMPLETE. Any other error propagates, a LoomServerError
    ledgered first."""
    from server.core.errors import IdentityIncompleteError

    try:
        target = gates.authz_target(
            op,
            org=getattr(input_obj, "org", None),
            namespace=getattr(input_obj, "namespace", None),
            auth=auth,
        )
        await gates.authorize(op, auth, target, transport="mcp")
    except PermissionError as e:
        _emit_refused(
            op.name, arguments, error_code=gates.ROLE_REFUSAL_CODE, request_id=request_id, error=e
        )
        return _ok(req_id, _forbidden_result(op, e, auth, target.org, target.namespace))
    except IdentityIncompleteError as e:
        # HTTP's 400 IDENTITY_INCOMPLETE, as a tool result: escaping, it
        # would become a bare HTTP 400 with no JSON-RPC frame and lose the batch.
        _emit_refused(op.name, arguments, error_code=e.code, request_id=request_id)
        return _ok(req_id, _refusal_result(e))
    except LoomServerError as e:
        # A read the gate could not make past its cache (a 503): counted
        # against the server, then answered as raised.
        _emit_refused(op.name, arguments, error_code=e.code, request_id=request_id, error=e)
        raise
    return target


def _emit_refused(
    op_name: str,
    arguments: dict[str, Any],
    *,
    error_code: str,
    request_id: str,
    error: BaseException | None = None,
) -> bool:
    """A call's row when it is refused before its handler runs: within a
    batch, a caller's refusal once and a server fault a few times, as
    `mcp_ledger` explains.
    False when the batch already wrote it, so the caller skips its other
    rows too."""
    from server.auth.fail_closed import answered_unverified
    from server.observability.sli import row_status
    from server.transport._common import ledger_identity

    org = ledger_identity(arguments)["org"]
    status = row_status(False, error_code, error, server_fault=answered_unverified())
    first = batch_allows_row(op_name, error_code, org, status)
    if first:
        _emit_mcp_event(
            op_name,
            arguments,
            0,
            success=False,
            error_code=error_code,
            request_id=request_id,
            error=error,
        )
    return first


def _refusal_result(e: BaseException) -> dict[str, Any]:
    """A 4xx LoomServerError as the `isError` result `_handler_failed` answers."""
    from server.transport.mcp_refusals import refusal_body, refusal_text

    body = refusal_body(e) or {"code": "BAD_REQUEST", "message": str(e)}
    return {
        "content": [{"type": "text", "text": refusal_text(body)}],
        "isError": True,
        "structuredContent": body,
    }


@dataclass(slots=True)
class _AdmittedCall:
    """What the phases after authorization share: the validated call, its
    identity and the namespace/org the limiter, telemetry and post-handler
    rows key on. Request-scoped; built once `_authorize_call` admits."""

    req_id: Any
    op: Operation
    input_obj: BaseModel
    arguments: dict[str, Any]
    auth: Any
    request_id: str
    mcp_session_id: str
    rl_ns: str
    rl_org: str
    target: gates.Target
    # The session's implicit run was stamped into `run_id` (not the caller's).
    stamped_run: bool = False


async def _admission_refusal(call: _AdmittedCall) -> dict[str, Any] | None:
    """Phase 6 of `_call_tool`: the traffic class, the read scope (which may
    rewrite `call.input_obj`), the history policy gate, then the rate
    limiter — HTTP's order after authorization. Returns the refusal
    response, or None to run the handler. The classifier and the limiter
    FAIL OPEN (gates); the policy gate turns only PolicyForbiddenError, and
    the read-scope gate only AuthInsufficientError and
    IdentityIncompleteError, into a refusal."""
    req_id, op, input_obj, arguments = call.req_id, call.op, call.input_obj, call.arguments
    auth, request_id, target = call.auth, call.request_id, call.target
    # Published for the ClickHouse settings composer, the ask handler's tier
    # policy and the ledger row; keys the limiter below.
    traffic_class, _source = await gates.traffic_class(op, auth, target, arguments)

    # Read scope first, as HTTP: the policy never evaluates a rule for a user the
    # token is not pinned to, and it sees the pinned input.
    from server.core.errors import AuthInsufficientError, IdentityIncompleteError

    try:
        call.input_obj = gates.read_scope(op, auth, input_obj)
    except (AuthInsufficientError, IdentityIncompleteError) as e:
        _emit_refused(op.name, arguments, error_code=e.code, request_id=request_id)
        return _ok(req_id, _refusal_result(e))

    # History policy, at parity with HTTP: after authz, before the handler.
    # Off → no-op; shadow → records; enforce → structured denial.
    from server.identity.app.policy_gate import PolicyForbiddenError as _PolicyForbidden

    try:
        await gates.policy(op, target, call.input_obj, arguments, request_id=request_id)
    except _PolicyForbidden as e:
        _emit_refused(
            op.name, arguments, error_code="POLICY_FORBIDDEN", request_id=request_id, error=e
        )
        return _ok(
            req_id,
            {
                "content": [{"type": "text", "text": e.message}],
                "isError": True,
                "structuredContent": e.to_detail(),
            },
        )
    rl = gates.rate_limit(op, auth, target, traffic_class)
    if rl is not None and not rl.allowed:
        # Wait out the hint once and re-check. The SDK honours Retry-After;
        # an MCP host does not, so without this every refusal would reach the
        # model as a failed tool call.
        rl = await _ratelimit_retry_once(rl, op, auth, target, traffic_class=traffic_class)
    if rl is not None and not rl.allowed:
        _emit_refused(op.name, arguments, error_code="RATE_LIMITED", request_id=request_id)
        wait = gates.retry_after_s(rl)
        msg = (
            f"namespace {target.named_namespace!r} exceeded {op.name} rate limit (retry in {wait}s)"
        )
        return _ok(
            req_id,
            {
                "content": [{"type": "text", "text": msg}],
                "isError": True,
                "structuredContent": {"error": msg, "code": "RATE_LIMITED", "retry_after_s": wait},
            },
        )
    return None
