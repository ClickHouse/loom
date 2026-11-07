"""The rows a successful call leaves after its handler.

A budget violation when the call ran over its budget (SLO alerts hang off
these), the op's audit row when it declares one, and the idempotency replay
row when the call carried a key. They are independent, so they go out in one
concurrent round instead of three awaited writes in a row. Both transports
write them through `post_handler_rows`. Each row is best-effort: a failure
is logged and never fails the call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from server.observability import audit

if TYPE_CHECKING:
    from server.core.idempotency import Scope
    from server.operations.base import Operation

log = logging.getLogger("server.transport.post_rows")


async def gather_best_effort(
    named: list[tuple[str, Awaitable[Any]]],
    *,
    op_name: str,
    transport: str = "http",
) -> None:
    """Await every row write at once; log (never raise) the ones that fail."""
    if not named:
        return
    results = await asyncio.gather(*(aw for _, aw in named), return_exceptions=True)
    for (what, _), res in zip(named, results, strict=True):
        if isinstance(res, BaseException):
            log.warning("%s %s failed for %s (non-fatal): %s", transport, what, op_name, res)


def _audit_subject(op: Operation, input_obj: Any, response_body: Any) -> dict[str, Any]:
    """The audit row's subject: the op's `audit_subject`, else the validated
    input with its text values hashed (`audit.input_subject`); {} when either
    fails (the row is still written)."""
    try:
        if op.audit_subject:
            return op.audit_subject(input_obj, response_body)
        subject = audit.input_subject(input_obj)
    except Exception:  # noqa: BLE001 — a subject that cannot be built still gets an audit row
        return {}
    return subject if isinstance(subject, dict) else {}


async def post_handler_rows(
    op: Operation,
    *,
    transport: str,
    request_id: str,
    org: str,
    namespace: str,
    agent: str,
    actor: str,
    input_obj: Any,
    elapsed_ms: int,
    response_body: Any,
    idem: tuple[Scope | None, str | None],
    replay_headers: dict[str, str],
) -> None:
    """The rows one successful call leaves, for either transport. `org` /
    `namespace` are the tenant the gate authorized (the request's, else the
    plane default a code.* / tracker.* op was admitted under); the audit and
    budget rows name it. `response_body` and `replay_headers` are what a
    replay of this call's idempotency key answers with."""
    from server.observability.budgets import effective_budget_ms
    from server.transport._common import emit_budget_violation, emit_op_audit

    rows: list[tuple[str, Awaitable[Any]]] = []
    try:
        # Per consumer, then per op, then the declaration.
        budget = effective_budget_ms(op.name, op.budget_p99_ms)
    except Exception as e:  # noqa: BLE001 — the budget lookup never fails a call
        log.warning("%s budget lookup failed for %s (non-fatal): %s", transport, op.name, e)
        budget = None
    if budget is not None and elapsed_ms > budget:
        rows.append(
            (
                "budget audit",
                emit_budget_violation(
                    op_name=op.name,
                    request_id=request_id,
                    org=org,
                    namespace=namespace,
                    agent=agent,
                    elapsed_ms=elapsed_ms,
                    budget_ms=budget,
                    transport=transport,
                ),
            )
        )
    if op.audit:
        rows.append(
            (
                "audit emit",
                emit_op_audit(
                    op_name=op.name,
                    request_id=request_id,
                    org=org,
                    namespace=namespace,
                    actor=actor,
                    subject=_audit_subject(op, input_obj, response_body),
                ),
            )
        )
    idem_scope, idem_body_hash = idem
    if op.idempotent and idem_scope is not None and idem_body_hash is not None:
        from server.core import idempotency as _idempotency

        rows.append(
            (
                "idempotency store",
                _idempotency.store(
                    idempotency_key=idem_scope.key,
                    org=idem_scope.org,
                    subject=idem_scope.subject,
                    body_hash=idem_body_hash,
                    operation=op.name,
                    status_code=200,
                    response_body=response_body,
                    response_headers=replay_headers,
                ),
            )
        )
    await gather_best_effort(rows, op_name=op.name, transport=transport)
