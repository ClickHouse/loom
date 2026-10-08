"""Per-stage wall clock for memory.search.

The search handler is a sequence of awaited stages — plan, embed, graph,
vector legs, rerank, assemble, the computed-evidence tail, bodies — and
the total alone does not say which one was slow. `StageClock.mark(name)`
charges the time since the previous mark to `stage_ms[name]` inside the
`retrieval_work` payload every response already carries, so a slow search
says WHICH stage was slow without a trace lookup.

`sample_loop_lag_ms()` is the second number the payload needs: how long an
`await asyncio.sleep(0)` took to come back. On an idle event loop that is
tens of microseconds; on a saturated worker it is the queueing delay every
awaited stage pays on resume. Without it a CPU-bound process reads as a slow
database (one slow search showed 1,727 ms wall against 88 ms of ClickHouse time).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class StageClock:
    """Charges elapsed wall time to named stages in a work dict.

    Marks are cumulative per name (a stage that runs twice adds up) and
    rounded to a tenth of a millisecond. `finish()` writes `total` from the
    clock's start, so the stages and the total share one origin.
    """

    __slots__ = ("_last", "_start", "_work")

    def __init__(self, work: dict[str, Any]) -> None:
        self._work = work
        self._start = time.perf_counter()
        self._last = self._start
        work.setdefault("stage_ms", {})

    def mark(self, name: str) -> float:
        now = time.perf_counter()
        ms = (now - self._last) * 1000.0
        self._last = now
        stages = self._work.setdefault("stage_ms", {})
        stages[name] = round(float(stages.get(name, 0.0)) + ms, 1)
        return ms

    def finish(self) -> float:
        total = (time.perf_counter() - self._start) * 1000.0
        self._work.setdefault("stage_ms", {})["total"] = round(total, 1)
        return total


async def sample_loop_lag_ms() -> float:
    """Wall time for one trip through the event loop's ready queue."""
    t0 = time.perf_counter()
    await asyncio.sleep(0)
    return round((time.perf_counter() - t0) * 1000.0, 2)
