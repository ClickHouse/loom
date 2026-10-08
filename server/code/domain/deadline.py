"""A request's wall-clock budget for code.grep and code.search.

A caller with a clock of its own (a review bot with a per-call timeout) sends
`deadline_ms`; the handler then stops starting new work when the budget runs
out and returns what it has, marked `partial: true` with a short
`partial_reason` naming what did not finish. A deadline never raises: a stage
it cuts contributes nothing, the way a stage that failed soft always has.

Unset, no budget exists and the request runs unbounded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# The request field's bounds: under 200 ms nothing useful finishes, and a
# minute is past any caller's patience.
MIN_MS = 200
MAX_MS = 60_000

# A statement's share of what is left, and the floor it never goes under:
# the rest pays for the reads that follow it (spans, as_of).
STATEMENT_SHARE = 0.8
STATEMENT_FLOOR_S = 0.05

# A statement stopped by `timeout_overflow_mode = 'break'` returns normally;
# a wall this close to its bound is read as stopped.
BROKE_AT = 0.9


@dataclass
class Budget:
    """`ms` from `started` (monotonic seconds). `cut` names what the budget
    dropped, in the order it happened."""

    ms: int
    started: float = field(default_factory=time.monotonic)
    cut: list[str] = field(default_factory=list)

    @property
    def until(self) -> float:
        return self.started + self.ms / 1000.0

    def left_ms(self) -> int:
        """Milliseconds left, never negative."""
        return max(0, int((self.until - time.monotonic()) * 1000))

    def allows(self, need_ms: int) -> bool:
        """True when at least `need_ms` are left."""
        return self.left_ms() >= need_ms

    def drop(self, what: str) -> None:
        """Record a stage or leg the budget cut (once each)."""
        if what and what not in self.cut:
            self.cut.append(what)

    @property
    def partial(self) -> bool:
        return bool(self.cut)

    def reason(self) -> str:
        """`deadline N ms: a; b`, or "" when nothing was cut."""
        if not self.cut:
            return ""
        return f"deadline {self.ms} ms: " + "; ".join(self.cut)

    def statement_s(self, reserve_ms: int = 0) -> float:
        """The `max_execution_time` for the next statement: its share of what
        is left after `reserve_ms`, at least the floor."""
        left = max(0, self.left_ms() - reserve_ms) / 1000.0
        return max(STATEMENT_FLOOR_S, round(left * STATEMENT_SHARE, 3))


def start(deadline_ms: int | None, *, started: float | None = None) -> Budget | None:
    """A budget for `deadline_ms`, or None when the request set none (or 0)."""
    if not deadline_ms:
        return None
    ms = max(MIN_MS, min(MAX_MS, int(deadline_ms)))
    return Budget(ms=ms, started=time.monotonic() if started is None else started)


def break_settings(max_execution_s: float | None) -> str:
    """The SETTINGS items that stop a scan at `max_execution_s` and return the
    rows read so far instead of raising; "" when unbounded. Rendered as a
    literal: ClickHouse takes no query parameters in SETTINGS."""
    if max_execution_s is None:
        return ""
    s = max(STATEMENT_FLOOR_S, float(max_execution_s))
    return f", max_execution_time = {s:.3f}, timeout_overflow_mode = 'break'"


def broke(elapsed_s: float, max_execution_s: float | None) -> bool:
    """True when a bounded statement ran to (about) its bound, so its rows
    may be a prefix of the answer."""
    return max_execution_s is not None and elapsed_s >= max_execution_s * BROKE_AT


__all__ = [
    "MAX_MS",
    "MIN_MS",
    "Budget",
    "break_settings",
    "broke",
    "start",
]
