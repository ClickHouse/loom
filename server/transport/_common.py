"""Shared HTTP/MCP transport helpers: request binding, plane defaults,
budget violations, op audits."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from server.auth.context import current_auth
from server.core.errors import (
    AuthInsufficientError,
    IdentityIncompleteError,
    loads_keeping_non_finite,
)
from server.identity.domain.read_scope import ReadFilters, request_read_filters

if TYPE_CHECKING:
    from server.core.request_work import RequestWork

# The actor recorded when a request carries no auth. One spelling for both
# transports, so unauthenticated traffic is one identity in every by-user view;
# the transport is already carried by the `loom.transport` label.
ANONYMOUS_ACTOR = "anonymous"


IDENTITY_FIELDS: tuple[str, ...] = ("org", "namespace", "agent", "user_id", "session_id")


def request_identity(args: dict[str, Any]) -> dict[str, str]:
    """The identity tuple as a request's raw arguments state it, for the
    ledger row and (HTTP) the authorization target: an absent or null field
    is the "-" sentinel — never the text "None" — and anything else its
    string form. Both transports read raw arguments through this."""
    out: dict[str, str] = {}
    for k in IDENTITY_FIELDS:
        v = args.get(k)
        out[k] = "-" if v is None else str(v)
    return out


def ledger_identity(args: dict[str, Any]) -> dict[str, str]:
    """`request_identity` with the identity a narrowed token acts as: the user (and
    session) its read scope pins, and always its `read_user_id` pin, so a refused
    call naming another user is logged as the caller, never as that user. A history
    rule keyed on the user counts the caller's own calls, whatever its body names.
    An org or namespace the body left out (absent to the gates: missing, blank
    or "-") is the one the gate admitted the call against, so a call served on
    a default is the tenant's row; one refused before that names the token's
    own org, unless the token is org-wide (`*`). Whatever stays absent is "-";
    a padded name is stripped."""
    from server.core import request_work
    from server.core.naming import SENTINEL, present

    out = request_identity(args)
    bag = request_work.current()
    admitted = bag.admitted if bag is not None else None
    auth = current_auth()
    token_org = present(auth.org) if auth is not None else None
    for k, fill in zip(("org", "namespace"), admitted or (SENTINEL, SENTINEL), strict=True):
        out[k] = present(out[k]) or fill
    if present(out["org"]) is None and token_org not in (None, "*"):
        out["org"] = token_org
    if auth is None:
        return out
    try:
        scope: ReadFilters | None = request_read_filters(auth)
    except (AuthInsufficientError, IdentityIncompleteError):
        scope = None
    if scope is not None and scope.narrowed:
        out["user_id"] = scope.user_id or out["user_id"]
        out["session_id"] = scope.session_id or out["session_id"]
    if auth.read_user_id:
        out["user_id"] = auth.read_user_id
    return out


# The request id is the second word of the query tag, so a caller's id is used only
# when it is one short token.
_CALLER_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def request_id_from(header: str | None) -> str:
    """The caller's `X-Request-Id` when it is 1-64 letters, digits, `.`, `_` or `-`;
    otherwise a fresh uuid, which the response echoes in its place."""
    if header and _CALLER_REQUEST_ID.fullmatch(header):
        return header
    return str(uuid.uuid4())


def bind_request(op_name: str, request_id: str) -> None:
    """Attribute the rest of this request to `request_id`: its log lines
    (`request_id_var`) and its ClickHouse queries (`<op> <request_id>` as
    `log_comment`), so a ledger row, a query_log row and a log line join on
    one id whichever transport served the call."""
    from server.core.ch import set_query_tag
    from server.core.logging_config import request_id_var

    request_id_var.set(request_id)
    set_query_tag(f"{op_name} {request_id}")


def sli_columns(op_name: str, transport: str) -> dict[str, Any]:
    """The ledger row's SLI columns for a call of `op_name` (`emit_agent_event`
    keywords): its transport, its SLO class and the budget it is held to."""
    from server.observability import sli
    from server.observability.budgets import effective_budget_ms
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME.get(op_name)
    if op is None:
        return {"transport": transport}
    return {
        "transport": transport,
        "slo_class": sli.slo_class(
            op.name,
            writes=not op.read_only,
            admin=op.required_role >= Role.ADMIN,
        ),
        "budget_ms": effective_budget_ms(op.name, op.budget_p99_ms),
    }


def begin_request() -> RequestWork:
    """Bind everything a request accumulates, in one call for both transports:
    the cost accumulator (tokens and dollars), the degradation notes (what the
    response did NOT do) and the work bag (ClickHouse cost, ask stages,
    injection counts, skipped legs). A fresh set per op, so nothing leaks
    across requests on a long-lived task."""
    from server.auth import fail_closed
    from server.core import request_work
    from server.memory.app.blacklist_guard import begin_request_budget
    from server.observability import cost_accumulator, degradations

    cost_accumulator.begin_op()
    degradations.begin()
    fail_closed.reset()
    begin_request_budget()
    return request_work.begin()


def begin_call() -> None:
    """`begin_request` for one MCP `tools/call`, and the request-scoped state a
    batch would carry over: it runs its entries in one task, so the previous
    entry's hint, traffic class, run and budget shape would otherwise ride
    every later row. Each HTTP request runs in its own context."""
    from server.observability.budget_shape import note_budget_shape
    from server.observability.ledger import set_hint_context
    from server.observability.run_context import set_ledger_run
    from server.observability.traffic_class import set_current_traffic_class

    begin_request()
    set_hint_context("")
    set_current_traffic_class("")
    set_ledger_run("")  # bound again once the call is admitted
    note_budget_shape("")


async def emit_budget_violation(
    *,
    op_name: str,
    request_id: str,
    org: str,
    namespace: str,
    agent: str,
    elapsed_ms: int,
    budget_ms: int,
    transport: str = "",
    consumer: str | None = None,
) -> None:
    """Best-effort `budget.violated` telemetry row (SLO alerts hang off these).

    The row is an event of kind `budget_violation` on the `loom.events`
    stream, not an audit_log row: a budget violation is a latency reading,
    not a memory-changing write, and audit rows feed an hourly checkpoint
    digest that exists to make state changes tamper-evident (violations
    would be about a third of them). `memory.budget_violations` and the
    SLO alerts read the stream (`admin_repo.budget_violations`). The
    identity columns are typed on the event; the reading rides the payload.

    `budget_ms` is the already-resolved effective budget. overage_ratio uses a
    finite sentinel (1e9) when budget_ms=0: float('inf') serializes to the
    non-standard JSON `Infinity`, which CH can't parse.

    `consumer` rides the event's payload; None reads the
    request-scoped value the transport published.
    """
    from server.observability import events_stream
    from server.observability.consumer import current_consumer

    if consumer is None:
        consumer = current_consumer()

    # An op whose budget is below its normal latency violates it on nearly
    # every request, and one INSERT per violation would sit on the request's
    # critical path. One row per op per interval carries the count of the
    # violations it stands for; the alert still fires, the write path does not
    # pay for a stale budget.
    suppressed = _budget_violation_gate(op_name, org, namespace)
    if suppressed is None:
        return
    overage_ratio = round(elapsed_ms / budget_ms, 3) if budget_ms > 0 else 1e9
    row: dict[str, Any] = {
        "event_type": BUDGET_VIOLATED_EVENT,
        "org": org,
        "namespace": namespace,
        "agent": agent,
        "actor": "control-plane",
        "request_id": request_id,
        "operation": op_name,
        "elapsed_ms": elapsed_ms,
        "budget_ms": budget_ms,
        "overage_ratio": overage_ratio,
    }
    if suppressed:
        row["suppressed_since_last"] = suppressed
    if transport:
        row["transport"] = transport
    if consumer:
        # Which client was slow; rides the event's payload.
        row["consumer"] = consumer
    events_stream.record(events_stream.BUDGET_VIOLATION_KIND, row)


BUDGET_VIOLATED_EVENT = "budget.violated"


_BUDGET_LAST_EMIT: dict[str, float] = {}
_BUDGET_WINDOW_ROWS: dict[str, int] = {}
_BUDGET_SUPPRESSED: dict[str, int] = {}


def _budget_violation_gate(
    op_name: str, org: str = "", namespace: str = "", *, now: float | None = None
) -> int | None:
    """Rate limit for `budget.violated` rows, keyed by (op, org, namespace) so
    a tenant's SLO signal is never hidden by another tenant's storm. Within
    each window of `budget_violation_audit_interval_s` the first
    `budget_violation_audit_burst` violations write their own row; the rest
    are counted and ride on the next row as `suppressed_since_last`. Returns
    that count when this violation should be written, None when it was
    counted. Interval 0 writes every violation."""
    import time

    from server.settings import settings

    interval = float(settings.budget_violation_audit_interval_s)
    burst = max(1, int(settings.budget_violation_audit_burst))
    key = f"{op_name}\x1f{org}\x1f{namespace}"
    t = time.monotonic() if now is None else now
    if interval <= 0:
        return 0
    start = _BUDGET_LAST_EMIT.get(key)
    if start is None or t - start >= interval:
        if len(_BUDGET_LAST_EMIT) > 10_000:  # bounded: bench namespace churn must not grow it
            _BUDGET_LAST_EMIT.clear()
            _BUDGET_WINDOW_ROWS.clear()
            _BUDGET_SUPPRESSED.clear()
        _BUDGET_LAST_EMIT[key] = t
        _BUDGET_WINDOW_ROWS[key] = 1
        return _BUDGET_SUPPRESSED.pop(key, 0)
    if _BUDGET_WINDOW_ROWS.get(key, 0) < burst:
        _BUDGET_WINDOW_ROWS[key] = _BUDGET_WINDOW_ROWS.get(key, 0) + 1
        return 0
    _BUDGET_SUPPRESSED[key] = _BUDGET_SUPPRESSED.get(key, 0) + 1
    return None


async def emit_op_audit(
    *,
    op_name: str,
    request_id: str,
    org: str,
    namespace: str,
    actor: str,
    subject: dict[str, Any],
) -> None:
    """Best-effort audit row for an op declared `audit=True`."""
    from server.observability import audit

    await audit.emit(
        event_type=op_name,
        org=org,
        namespace=namespace,
        actor=actor,
        subject=subject,
        request_id=request_id,
    )


class ValidationError(Exception):
    """The ledger's copy of pydantic's ValidationError:
    the same class name for `args.error_class`, and a message that is
    `field: message` per error rather than pydantic's "N validation errors
    for Model" headline — so `bounded_error`'s first line names the field
    that failed."""


def validation_note(errors: list[dict[str, Any]]) -> Exception:
    from server.observability.event_args import validation_error_text

    return ValidationError(validation_error_text(errors))


def validation_errors(e: Any) -> list[dict[str, Any]]:
    """A pydantic ValidationError as JSON-safe dicts for a 422 body.

    `e.errors()` keeps the raising exception object under `ctx.error` for a
    `value_error` (any `@model_validator` / `@field_validator` that raises
    ValueError), and the response encoder then fails on it — the client would see
    a 500 for what is a 422. `e.json()` is the serialiser pydantic itself
    guarantees; round-tripping through it drops nothing a client can use. A
    non-finite input (`1e400`) comes back as a string: JSON has no inf."""
    try:
        return list(loads_keeping_non_finite(e.json(include_url=False)))
    except Exception:  # noqa: BLE001 — never let the error report itself fail
        return [{"type": "validation_error", "msg": str(e)}]
