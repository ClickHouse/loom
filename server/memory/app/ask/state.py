"""Per-request state of one memory.ask that crosses call boundaries without a
parameter: the overrides `ask` sets and the synthesizer reads, the telemetry
the synthesizer writes and the result reads."""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any

_RECOUNT_OVERRIDE: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "loom_recount_override", default=None
)
_PERSONALIZE_OVERRIDE: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "loom_personalize_override", default=None
)

_LAST_COUNT_RECOMPUTED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "loom_last_count_recomputed", default=False
)

_LAST_SYNTH_MODEL: contextvars.ContextVar[str] = contextvars.ContextVar(
    "loom_last_synth_model", default=""
)

# Synthesis-family calls made on the current ask (primary, deep, escalation,
# the fallbacks); ask() zeroes it, `_synthesize` counts, the wire reads it.
_SYNTH_CALLS: contextvars.ContextVar[int] = contextvars.ContextVar(
    "memory_ask_synth_calls", default=0
)


def _set_request_overrides(recount: bool | None, personalize: bool | None) -> None:
    """The per-call switches the synthesizer reads without a parameter."""
    # A per-call recount override rides a ContextVar so the
    # synthesizer, four call sites down, reads it without a new parameter.
    _RECOUNT_OVERRIDE.set(recount)
    _reset_synth_telemetry()
    _PERSONALIZE_OVERRIDE.set(personalize)


def _reset_synth_telemetry() -> None:
    """A new ask: no synthesis-family call yet, no prompt block accounted."""
    from server.memory.domain import prompt_blocks as _pb

    _SYNTH_CALLS.set(0)
    _pb.reset()


def _synth_telemetry() -> dict[str, Any]:
    """What the ask's synthesis calls cost in prompt, for the result: the call
    count and the per-block token estimate."""
    from server.memory.domain import prompt_blocks as _pb

    return {"synth_calls": int(_SYNTH_CALLS.get() or 0), "prompt_blocks": _pb.current()}


def _premise_verdict_of_last_call() -> str:
    """The Premise line of the synthesis call that produced the answer,
    "" when the line was off or did not parse."""
    from server.memory.domain.premise_line import LAST_PREMISE

    return str(LAST_PREMISE.get() or "")


def _retain_task_result(t: asyncio.Task[Any]) -> None:
    """Consume a background task's outcome so an unobserved exception is
    never logged as 'Task exception was never retrieved'."""
    if not t.cancelled():
        t.exception()
