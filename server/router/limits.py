"""Per-key call limits for the router's rate-limited answers: a cell's placement calls
and a client's lookups. Each router replica counts on its own, so a deployment of N
replicas admits up to N times the limit; the directory's quota (`placements_max`) is
the bound that holds across them."""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable

# Keys one limiter remembers: past it, the key idle longest is forgotten (a client
# address that comes back starts a fresh window).
MAX_KEYS = 10_000  # not a setting: memory per replica, not behaviour


class Limiter:
    """At most `per_minute` calls per key in any 60 seconds."""

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._calls: OrderedDict[str, deque[float]] = OrderedDict()

    def admit(self, key: str) -> bool:
        """Count a call for `key` if it is within the limit; whether it was."""
        now = self._clock()
        calls = self._calls.pop(key, None) or deque()
        while calls and now - calls[0] >= 60.0:
            calls.popleft()
        admitted = len(calls) < self.per_minute
        if admitted:
            calls.append(now)
        self._calls[key] = calls  # most recently used last
        while len(self._calls) > MAX_KEYS:
            self._calls.popitem(last=False)
        return admitted

    def wait_s(self, key: str) -> int:
        """Whole seconds until `key`, just refused by `admit`, is admitted again, at least
        1 (for Retry-After)."""
        oldest = self._calls[key][0]
        return max(1, math.ceil(60.0 - (self._clock() - oldest)))
