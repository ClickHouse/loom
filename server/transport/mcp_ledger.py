"""The ledger rows MCP writes for calls it refuses before their handler runs.

A refusal before dispatch has no bound caller, so its row is the token's:
its org (or the one the screen authorized), subject and consumer, `-` for
the rest, no arguments (`emit_refusal`). Within one JSON-RPC batch, every
refusal before a handler runs (these, and `mcp_call`'s argument, gate,
policy and rate-limit refusals) is written once per (op, code, org, status
class): the rows come before the rate limiter and a batch has no length
cap, so one POST of many refused entries could otherwise fill the ledger
queue for every tenant. A handler's failure is always its own row.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from server.core.naming import SENTINEL
from server.observability.sli import status_class

# The refusals a batch has written, while one runs.
_batch_refusals: contextvars.ContextVar[set[tuple[str, str, str, str]] | None] = (
    contextvars.ContextVar("mcp_batch_refusals", default=None)
)


@contextmanager
def one_refusal_row_each() -> Iterator[None]:
    """For one batch: a refusal repeated across its entries is ledgered once."""
    token = _batch_refusals.set(set())
    try:
        yield
    finally:
        _batch_refusals.reset(token)


def first_in_batch(op_name: str, code: str, org: str, status_class: str) -> bool:
    """False when this batch has already written this refusal."""
    seen = _batch_refusals.get()
    if seen is None:
        return True
    key = (op_name, code, org, status_class)
    if key in seen:
        return False
    seen.add(key)
    return True


def emit_refusal(
    op_name: str, auth: Any, *, error_code: str, org: str = "", server_fault: bool = False
) -> None:
    """Emit a token-attributed refusal row in an isolated request context,
    unless this batch already wrote it. The copy is reset so earlier batch
    entries cannot contaminate the row."""

    org = org or str(getattr(auth, "org", "") or "")
    if org in ("", "*"):
        org = SENTINEL
    status = "server" if server_fault else status_class(False, error_code, None)
    if not first_in_batch(op_name, error_code, org, status):
        return
    contextvars.copy_context().run(_emit_refusal, op_name, auth, error_code, org, server_fault)


def _emit_refusal(op_name: str, auth: Any, code: str, org: str, server_fault: bool) -> None:
    from server.observability.consumer import bind_current_consumer
    from server.observability.ledger import emit_agent_event
    from server.observability.traffic_class import bind_claimed_class
    from server.transport._common import begin_call, sli_columns

    begin_call()
    bind_current_consumer(None, auth)
    bind_claimed_class(auth)
    emit_agent_event(
        op_name=op_name,
        identity={
            "org": org,
            "namespace": SENTINEL,
            "agent": SENTINEL,
            "user_id": SENTINEL,
            "session_id": SENTINEL,
        },
        request_id=str(uuid.uuid4()),
        latency_ms=0,
        success=False,
        error_code=code,
        subject=str(getattr(auth, "subject", "") or "") or None,
        server_fault=server_fault,
        **sli_columns(op_name, "mcp"),
    )
