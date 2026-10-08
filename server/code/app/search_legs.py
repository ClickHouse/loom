"""The code.search leg phase under a deadline.

Under one `asyncio.gather` the request waits for the slowest leg — usually the
hypothetical-answer leg (HyDE: a model-written passage, a second provider embed and a
full-precision scan — p50 2.1 s, p99 9.4 s) or a vector read queued behind a busy
replica. A caller with a budget (a PR review against a clock) needs to say "give me what
you have at 1.5 s".

`run_legs` runs the same legs beside each other and returns the same values
in the same order. Two bounds, both off by default so a request that sets
neither waits for every leg:

* `deadline_ms`: the leg phase's wall — what the request's budget has left
  after the ranking and the page are paid for. A leg still running at it is
  cancelled, contributes its empty value, and is named in `legs_timed_out`;
  the answer's `epistemic` reads `partial`. The deadline does not empty an
  answer a core leg was about to give: when neither the lexical nor the
  vector leg has returned, the first of them to return is waited for (the
  floor) up to `floor_ms` more, and only then are the others dropped.
* `code_search_llm_leg_grace_ms`: how long a model-backed leg (HyDE) may run
  past the last plain leg's return. The plain legs' answer is complete by
  then; the model leg either lands inside the grace or is dropped the same
  way.

Legs already fail soft (each catches its own error and returns its empty
value), so a dropped leg degrades exactly the way a failed one always has.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from typing import Any

from server.settings import settings

# The legs whose rows the answer cannot do without; the deadline waits for
# the first of them rather than serve nothing.
CORE_LEGS = ("lexical", "vector")
# Model-backed legs: bounded by the grace past the plain legs.
LLM_LEGS = ("hyde",)

# What a request budget must have left at the legs for a model-backed leg to
# start (HyDE's passage alone is ~2 s at p50); under it the leg is skipped.
MODEL_LEG_MIN_MS = 2500
# Of a request budget, what the legs leave for fusion, ranking and the page:
# a quarter, within these bounds.
POST_LEG_RESERVE_MS = (150, 2000)


def resolve_deadline_ms(requested: int | None) -> int:
    """The request budget in ms, else the deployment default; 0 = none."""
    if requested is not None:
        return max(0, int(requested))
    return max(0, int(settings.code_search_deadline_ms))


def leg_window(budget: Any) -> tuple[int | None, int | None]:
    """(leg deadline, floor) in ms for a request budget: the legs get what is left
    less the post-leg reserve (at least 1 ms), and the floor may wait out the rest.
    (None, None) without a budget."""
    if budget is None:
        return None, None
    lo, hi = POST_LEG_RESERVE_MS
    reserve = max(lo, min(hi, budget.ms // 4))
    left = budget.left_ms()
    legs = max(1, left - reserve)
    return legs, max(0, left - legs)


def llm_legs_off(llm_legs: str | None) -> bool:
    """True when the request (else the deployment) turns the model-backed
    legs off: `none` skips HyDE and a model-named concept expansion; the
    card-derived concept source (`cards:N`) calls no model and stays."""
    mode = str(llm_legs or settings.code_search_llm_legs_default or "auto")
    return mode.strip().lower() == "none"


async def run_legs(
    legs: dict[str, tuple[Awaitable[Any], Any]],
    *,
    result: Any,
    work: dict[str, Any],
    deadline_ms: int | None = None,
    floor_ms: int | None = None,
) -> list[Any]:
    """Run `{name: (awaitable, empty value)}` beside each other; return the
    values in the dict's order. `deadline_ms` None or 0 = no leg deadline;
    `floor_ms` None = the floor waits as long as the first core leg takes. A
    leg dropped by a bound yields its empty value and is named on
    `result.legs_timed_out` (with `result.epistemic = "partial"` and
    `work["legs_timed_out"]`, a count)."""
    names = list(legs)
    tasks = {n: asyncio.ensure_future(legs[n][0]) for n in names}
    try:
        return await _await_legs(
            legs, tasks, result=result, work=work, deadline_ms=deadline_ms, floor_ms=floor_ms
        )
    except BaseException:
        # the caller was cancelled, or a leg raised: no sibling leg keeps
        # reading after the request has gone
        for t in tasks.values():
            t.cancel()
        raise


async def _await_legs(
    legs: dict[str, tuple[Awaitable[Any], Any]],
    tasks: dict[str, asyncio.Task[Any]],
    *,
    result: Any,
    work: dict[str, Any],
    deadline_ms: int | None,
    floor_ms: int | None,
) -> list[Any]:
    names = list(legs)
    deadline = max(0, int(deadline_ms or 0))
    grace = max(0, int(settings.code_search_llm_leg_grace_ms))
    if not deadline and not grace:
        # no bound: one gather over every leg
        values = list(await asyncio.gather(*tasks.values()))
        _note_shape(work, [])
        return values
    t0 = time.perf_counter()
    until = (t0 + deadline / 1000.0) if deadline else None

    def _left(cap_s: float | None = None) -> float | None:
        rem = None if until is None else max(0.0, until - time.perf_counter())
        if cap_s is None:
            return rem
        return cap_s if rem is None else min(rem, cap_s)

    plain = [tasks[n] for n in names if n not in LLM_LEGS]
    model = [tasks[n] for n in names if n in LLM_LEGS]
    if plain:
        await asyncio.wait(plain, timeout=_left())
    pending_model = [t for t in model if not t.done()]
    if pending_model:
        await asyncio.wait(pending_model, timeout=_left(grace / 1000.0 if grace else None))
    core = [tasks[n] for n in names if n in CORE_LEGS]
    if core and not any(t.done() for t in core):
        # the floor: an answer from at least one content leg, inside the budget
        await asyncio.wait(
            core,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=None if floor_ms is None else floor_ms / 1000.0,
        )
    dropped = [n for n in names if not tasks[n].done()]
    for n in dropped:
        tasks[n].cancel()
    for n in dropped:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tasks[n]
    if dropped:
        result.legs_timed_out = dropped
        result.epistemic = "partial"
        work["legs_timed_out"] = len(dropped)
        budget = getattr(result, "budget", None)
        if budget is not None:
            budget.drop("legs not finished: " + ", ".join(dropped))
    work["leg_phase_ms"] = int((time.perf_counter() - t0) * 1000)
    _note_shape(work, dropped)
    return [legs[n][1] if n in dropped else tasks[n].result() for n in names]


def _note_shape(work: dict[str, Any], dropped: list[str]) -> None:
    """The budget shape this request is priced by: a request that reached the legs is
    `hybrid`, or `hybrid_hyde` when the model-backed leg ran and was waited for (a HyDE
    the deadline dropped was bounded like a plain one). The op's base budget prices what
    never gets here: the exact symbol route, an abstain before the legs, a memo hit."""
    from server.observability.budget_shape import note_budget_shape

    note_budget_shape(
        "hybrid_hyde" if work.get("hyde_queries") and "hyde" not in dropped else "hybrid"
    )
