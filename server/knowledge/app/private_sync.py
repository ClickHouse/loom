"""Diagnostic and billing boundaries for work on mirrored source documents."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from server.core import diagnostics
from server.core.naming import is_mirror_name
from server.llm import customer
from server.observability import cost_accumulator, provider_health
from server.workers import pass_context
from server.workers.cost_event import write_private_cost_event_for


@contextmanager
def scope() -> Iterator[None]:
    """Enter before any private listing, lease, fetch or cleanup work."""
    ctx = pass_context.current()
    if ctx is not None:
        ctx.private_work = True
    with diagnostics.private_work(), cost_accumulator.isolated_op(), provider_health.unrecorded():
        yield


@asynccontextmanager
async def mirror_costs(org: str, mirror: str, worker: str) -> AsyncIterator[None]:
    """File spend under the mirror, never in the enclosing pass's accumulator."""
    if not diagnostics.codes_only() or not is_mirror_name(mirror):
        raise ValueError("private billing requires a mirror and a private scope")
    started = time.monotonic()
    with cost_accumulator.isolated_op(), customer.for_org(org, namespace=mirror):
        try:
            yield
        finally:
            await write_private_cost_event_for(
                org=org, namespace=mirror, operation=f"worker.{worker}", started=started
            )
