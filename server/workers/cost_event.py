"""The worker rows of `agent_events`: the spend a pass, or one item inside it,
ran up on LLM and embedding calls.

Workers never pass through the transports' `emit_agent_event`, so this module
is also where worker spend reaches the `loom.cost.usd` / `loom.tokens`
counters (`metrics.record_cost`). Every emit drains the accumulator it reads,
so a later reader of the same context (the supervisor's pass row, or the
transport when `code.refresh wait=true` awaits a pass inside a request)
counts only what was left.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


def spent(snap: Any) -> bool:
    """Whether a drained accumulator carries any spend: dollars, a call or tokens."""
    return bool(
        float(getattr(snap, "cost_usd", 0.0) or 0.0) > 0
        or int(getattr(snap, "llm_calls", 0) or 0) > 0
        or int(getattr(snap, "embed_tokens", 0) or 0) > 0
        or int(getattr(snap, "llm_input_tokens", 0) or 0) > 0
        or int(getattr(snap, "llm_output_tokens", 0) or 0) > 0
    )


def emit_worker_event(
    snap: Any,
    *,
    org: str,
    namespace: str,
    operation: str,
    started: float,
    success: bool = True,
    error_code: str = "",
    args: dict[str, str] | None = None,
    latency_ms: int | None = None,
    agent: str = "-",
    user_id: str = "-",
    session_id: str = "-",
) -> None:
    """One `kind='worker'` AgentEvent carrying `snap`'s spend (a drained
    accumulator, or None for none), and the same spend on the cost counters.
    Raises what the emitter raises; callers are fail-quiet."""
    from server.observability import metrics
    from server.observability.events import get_emitter

    event = _worker_event(
        snap,
        org=org,
        namespace=namespace,
        operation=operation,
        started=started,
        success=success,
        error_code=error_code,
        args=args,
        latency_ms=latency_ms,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
    )
    get_emitter().emit(event)
    metrics.record_cost(
        operation,
        namespace=namespace,
        cost_usd=event.upstream_cost_usd,
        embed_tokens=event.embedding_tokens,
        llm_input_tokens=event.input_tokens,
        llm_output_tokens=event.output_tokens,
    )


def _worker_event(
    snap: Any,
    *,
    org: str,
    namespace: str,
    operation: str,
    started: float,
    success: bool = True,
    error_code: str = "",
    args: dict[str, str] | None = None,
    latency_ms: int | None = None,
    agent: str = "-",
    user_id: str = "-",
    session_id: str = "-",
) -> Any:
    from server.observability.events import AgentEvent

    cost = float(getattr(snap, "cost_usd", 0.0) or 0.0)
    embed_tokens = int(getattr(snap, "embed_tokens", 0) or 0)
    llm_in = int(getattr(snap, "llm_input_tokens", 0) or 0)
    llm_out = int(getattr(snap, "llm_output_tokens", 0) or 0)
    return AgentEvent(
        ts=datetime.now(UTC),
        org=org,
        namespace=namespace,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        operation=operation,
        request_id="",
        latency_ms=int((time.monotonic() - started) * 1000) if latency_ms is None else latency_ms,
        success=success,
        error_code=error_code,
        kind="worker",
        args=dict(args or {}),
        input_tokens=llm_in,
        output_tokens=llm_out,
        embedding_tokens=embed_tokens,
        upstream_cost_usd=cost,
        model=snap.dominant_model() if snap is not None else "",
        llm_calls=int(getattr(snap, "llm_calls", 0) or 0),
    )


def emit_worker_cost_event_for(
    *,
    org: str,
    namespace: str,
    operation: str,
    started: float,
    agent: str = "-",
    user_id: str = "-",
    session_id: str = "-",
) -> None:
    """Drain the active cost accumulator into a worker event tagged with a
    real (org, namespace), when it holds any spend, and with the agent, user and
    session of the request the work was for when the worker knows them. Fail-quiet:
    a cost-event insert failing must never interrupt the worker."""
    try:
        from server.observability import cost_accumulator

        acc = cost_accumulator.current()
        if acc is None:
            return
        snap = acc.drain()
        if not spent(snap):
            return
        emit_worker_event(
            snap,
            org=org,
            namespace=namespace,
            operation=operation,
            started=started,
            agent=agent,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        log.warning("cost-event emit failed for %s", operation, exc_info=True)


async def write_private_cost_event_for(
    *, org: str, namespace: str, operation: str, started: float
) -> None:
    """Drain one mirror's spend directly to its ledger, with bounded best-effort I/O."""
    import asyncio

    from server.observability import cost_accumulator
    from server.observability.events import write_private_event

    acc = cost_accumulator.current()
    snap = acc.drain() if acc is not None else None
    if not spent(snap):
        return
    try:
        # not a setting: best-effort private accounting cannot delay turn cleanup indefinitely.
        async with asyncio.timeout(5.0):
            await write_private_event(
                _worker_event(
                    snap, org=org, namespace=namespace, operation=operation, started=started
                )
            )
    except Exception:  # noqa: BLE001 — telemetry failure must not fail a private turn
        pass
