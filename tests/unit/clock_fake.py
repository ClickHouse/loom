"""A manual clock for code that reads time through a seam.

    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.asyncio, "sleep", clock.async_sleep)
    ...
    assert clock.sleeps == [0.5, 1.0]

Time moves only when the test or the code under test says so: `advance`,
`sleep` and `async_sleep` add to it, and the sleeps are recorded. A backoff,
deadline or TTL test then asserts exact values instead of racing the wall
clock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class ManualClock:
    t: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock does not run backwards")
        self.t += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(max(0.0, seconds))

    async def async_sleep(self, seconds: float) -> None:
        """Advances without waiting, then yields once so other tasks run."""
        self.sleep(seconds)
        await asyncio.sleep(0)
