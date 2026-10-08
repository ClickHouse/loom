"""Memory expiry sweep worker.

Each pass applies every expired row's post-action. The pass itself
(`memory/app/expiry.py`) also serves the ADMIN op `memory.sweep_expired`;
this loop builds its config out of settings each pass and records the sweep
event.
"""

from __future__ import annotations

import time

from server.memory.app.expiry import ExpiryConfig, emit_sweep_event
from server.memory.app.expiry import run_pass as _expiry_pass
from server.settings import settings


async def run_pass() -> None:
    t0 = time.monotonic()
    res = await _expiry_pass(
        ExpiryConfig(
            batch_size=settings.memory_expiry_sweep_batch_size,
            demote_factor=settings.memory_expiry_demote_factor,
            demote_floor=settings.memory_expiry_demote_floor,
        )
    )
    emit_sweep_event(res, latency_ms=int((time.monotonic() - t0) * 1000))
