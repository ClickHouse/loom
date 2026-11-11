"""The agent_events ledger row: one per served request, and the rows a
service writes for work it finished after the request answered.

`emit_agent_event` drains the request's cost accumulator and work bag,
projects the allow-listed arguments, and stamps the attribution columns
(subject, consumer, traffic class, run, trace id) from the request context
both transports publish, so a caller outside a request resolves them the same
way.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from server.observability.event_args import redact_args
from server.observability.events import AgentEvent, get_emitter

# The mentor hint the current response carries (`out_hint_code`); the
# transports set it per call, '' when none.
_hint_code_var: ContextVar[str] = ContextVar("loom_mentor_hint", default="")


# Takes an event the request must commit before it answers (the policy witness,
# installed by the transports); None or False: the event is queued.
_event_hold: Callable[[AgentEvent], bool] | None = None


def set_event_hold(hold: Callable[[AgentEvent], bool] | None) -> None:
    global _event_hold
    _event_hold = hold


def set_hint_context(code: str) -> None:
    _hint_code_var.set(code or "")


def current_hint_code() -> str:
    return _hint_code_var.get()


def run_id_of(raw_args: dict[str, Any] | None) -> str:
    """The run a request names, as the ledger column stores it.

    Identity-class, so project_args strips it from `args`; it rides its own
    column instead. Only a string or int is accepted, bounded, '-' (the
    SDK's "no run" sentinel) and whitespace count as no run.

    A request that names no run of its own takes the transport-bound ledger
    run (the MCP session's implicit run, or the SDK's ambient
    run sent as `X-Loom-Run-Id`) — a label for the row, never a filter."""
    from server.observability.run_context import clean_run_id, current_ledger_run

    raw = (raw_args or {}).get("run_id")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return current_ledger_run()
    return clean_run_id(str(raw)) or current_ledger_run()


def current_trace_id() -> str:
    """The active OTel trace id as 32 hex chars, '' when none. It fills
    `agent_events.trace_id`: the same id the trace backend uses, one join key
    across ClickHouse and the traces."""
    try:
        from opentelemetry import trace as _trace

        _ctx = _trace.get_current_span().get_span_context()
        if _ctx and _ctx.is_valid:
            return format(_ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001 — a missing trace id must not drop the event
        pass
    return ""


def _current_subject() -> str:
    """The verified subject of the request being served, '' outside one."""
    from server.auth.context import current_auth as _current_auth

    auth = _current_auth()
    return str(getattr(auth, "subject", "") or "") if auth is not None else ""


def _attribution_fields(
    raw_args: dict[str, Any] | None, consumer: str | None, traffic_class: str | None
) -> dict[str, str]:
    """The ledger's attribution columns for one event.

    `consumer`: a caller's explicit value wins; else a token claim binds the
    tag (`claimed`), the request's own field is `self-reported`, the subject
    class is `class`. The transport's ContextVar carries the resolution it
    made; a caller outside one (a test, a worker) resolves here from the same
    three inputs. `traffic_class`: the class the transport resolved (claim →
    namespace row → hint → interactive); '' outside a request."""
    consumer_source = ""
    if consumer is None:
        from server.auth.context import current_auth as _current_auth
        from server.observability.consumer import (
            claimed_consumer,
            current_consumer,
            current_consumer_source,
            explicit_consumer,
            resolve_consumer_and_source,
        )

        _auth = _current_auth()
        if claimed_consumer(_auth) or explicit_consumer(raw_args) or not current_consumer():
            consumer, consumer_source = resolve_consumer_and_source(raw_args, _auth)
        else:
            consumer, consumer_source = current_consumer(), current_consumer_source()
    if traffic_class is None:
        from server.observability.traffic_class import current_traffic_class

        traffic_class = current_traffic_class()
    return {
        "consumer": consumer or "",
        "consumer_source": consumer_source,
        "traffic_class": traffic_class or "",
    }


def emit_agent_event(
    *,
    op_name: str,
    identity: dict[str, Any],
    request_id: str,
    latency_ms: int,
    success: bool,
    error_code: str,
    memory_key: str = "",
    impersonated_by: str = "",
    raw_args: dict[str, Any] | None = None,
    kind: str = "request",
    result: Any = None,
    error: BaseException | str | None = None,
    consumer: str | None = None,
    traffic_class: str | None = None,
    subject: str | None = None,
    transport: str = "",
    slo_class: str = "",
    budget_ms: int = 0,
    server_fault: bool = False,
) -> None:
    """Drain the op-scoped cost accumulator and emit one AgentEvent.

    `subject`: the verified token subject the row
    is attributed to. None resolves it from the request's auth context, so
    both transports agree without passing it; '' outside a request.

    `consumer`: the client the request spoke for, its own
    ledger column. None resolves it here — the request's `consumer` argument,
    else the request-scoped value the transport published, else the verified
    token's subject class — so both transports agree without passing it.

    `error`: the exception a failed request ended with. Its
    class name and a bounded, quote-redacted first line ride the row as
    `args.error_class` / `args.error`, so an INTERNAL_ERROR is attributable
    from the ledger without a trace lookup. Ignored on success.

    `transport`, `slo_class`, `budget_ms`: the SLI columns a transport fills
    (`server.observability.sli`); the row's `status_class` is derived here.
    `server_fault`: the row is the server's failure whatever its code says: a
    refusal recorded without its exception, or an answer given as if refused
    because a read could not be made (`row_in_scope`'s found=False).

    Absent accumulator → zero cost/token attribution. `raw_args` is the
    request's argument object; only its allow-listed projection reaches the
    row (event_args.project_args). Never the text.
    """
    from server.observability import cost_accumulator, sli
    from server.observability.event_args import (
        bounded_error,
        project_args,
        project_result,
        response_tokens_of,
    )

    acc = cost_accumulator.current()
    args: dict[str, str] = {}
    run_id = run_id_of(raw_args)
    if raw_args:
        # A request addressed at a restricted namespace
        # leaves no text in the ledger — identifiers and the digest stay.
        args = project_args(
            redact_args(
                raw_args, str(identity.get("org") or ""), str(identity.get("namespace") or "")
            )
            or {}
        )
    # A row with no request arguments of its own still names what it
    # returned: the deferred consistency verdict is such a
    # row, and its `out_consistency` is the label every analysis groups by.
    if result is not None:
        args.update(project_result(result, op_name=op_name))
    # The request's own work bag — ClickHouse rows
    # and bytes from every statement's response summary, identifier
    # injection counts, legs skipped under schema skew, and the ask-side
    # stage partition when the result carries none of its own.
    try:
        from server.core import request_work as _request_work
        from server.observability.event_args import (
            request_work_columns,
            request_work_stage_columns,
        )

        _bag = _request_work.current()
        if _bag is not None:
            args.update(request_work_columns(_bag))
            if "out_stage_ms" not in args:
                args.update(request_work_stage_columns(_bag))
    except Exception:  # noqa: BLE001 — telemetry never drops the event
        pass
    # The mentor hint this response carried, so "was the
    # hinted op called later in the session" is one query per code.
    _hint = current_hint_code()
    if _hint and "out_hint_code" not in args:
        args["out_hint_code"] = _hint
    if not success and error is not None:
        _cls, _msg = bounded_error(error)
        args["error_class"] = _cls
        if _msg:
            args["error"] = _msg
    trace_id = current_trace_id()
    attribution = _attribution_fields(raw_args, consumer, traffic_class)
    if subject is None:
        subject = _current_subject()
    if attribution["consumer_source"]:
        args["consumer_source"] = attribution["consumer_source"]

    event = AgentEvent(
        ts=datetime.now(UTC),
        trace_id=trace_id,
        org=identity["org"],
        namespace=identity["namespace"],
        agent=identity["agent"],
        user_id=identity["user_id"],
        session_id=identity["session_id"],
        run_id=run_id,
        operation=op_name,
        request_id=request_id,
        latency_ms=latency_ms,
        success=success,
        error_code=error_code,
        memory_key=memory_key,
        impersonated_by=impersonated_by,
        subject=subject,
        kind=kind,
        args=args,
        upstream_cost_usd=(acc.cost_usd if acc else 0.0),
        embedding_tokens=(acc.embed_tokens if acc else 0),
        input_tokens=(acc.llm_input_tokens if acc else 0),
        output_tokens=(acc.llm_output_tokens if acc else 0),
        model=(acc.dominant_model() if acc else ""),
        llm_calls=(acc.llm_calls if acc else 0),
        consumer=attribution["consumer"],
        # What the agent has to read, from the result both
        # transports already hand this function; 0 on failure.
        response_tokens=(response_tokens_of(result) if success else 0),
        traffic_class=attribution["traffic_class"],
        transport=transport,
        slo_class=slo_class,
        status_class=sli.row_status(success, error_code, error, server_fault=server_fault),
        budget_ms=budget_ms,
    )
    # The same row on both paths: the policy witness writes the one it holds itself.
    from server.observability.events import for_this_cell

    event = for_this_cell(event)
    if _event_hold is None or not _event_hold(event):
        get_emitter().emit(event)
    # Same numbers as OTel counters, so spend trends live beside latency and
    # retrieval quality instead of only in agent_events. Both transports reach
    # here, so this covers HTTP and MCP in one place. Never raises.
    try:
        from server.observability import metrics as _metrics

        if not success and acc is not None and acc.cost_usd > 0:
            _metrics.record_paid_error(
                op_name, namespace=identity["namespace"], error_code=error_code
            )
        _metrics.record_cost(
            op_name,
            namespace=identity["namespace"],
            cost_usd=(acc.cost_usd if acc else 0.0),
            embed_tokens=(acc.embed_tokens if acc else 0),
            llm_input_tokens=(acc.llm_input_tokens if acc else 0),
            llm_output_tokens=(acc.llm_output_tokens if acc else 0),
        )
    except Exception:  # noqa: BLE001 — cost metrics must never fail a request
        pass
