"""One process's copy of a small table that a fail-closed check reads on
every call.

The whole table is read at most once per `refresh_s`, counted from the end of
the last read. A change made in this process asks for the next read at once
(`forget`). A copy older than `max_stale_s` is not served: `get` answers None
and the caller refuses.

While a read is in progress, a call is served the copy in hand when that copy
is still young enough and no change made here is waiting to be read; otherwise
it waits on the lock for the read. A cancelled read schedules nothing, so the
next call reads. A change made here while a read ran may postdate what it
read, so the next call reads again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable


class PolledCopy[T]:
    def __init__(
        self, name: str, empty: T, read: Callable[[], Awaitable[T]], log: logging.Logger
    ) -> None:
        self.name = name
        self._value = empty
        self._read = read
        self._log = log
        # When the copy was read (monotonic, from the start of its query; 0.0 =
        # never), when the next read is due (0.0 = now), and when a change here
        # last asked for one.
        self._read_at = 0.0
        self._next_read = 0.0
        self._forgot_at = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self, now: float, max_stale_s: float) -> bool:
        return bool(self._read_at) and now - self._read_at <= max_stale_s

    async def get(self, *, refresh_s: float, max_stale_s: float) -> T | None:
        """The copy, read first when a read is due; None when it is too old to
        serve (never read, or every read since `max_stale_s` ago failed)."""
        now = time.monotonic()
        serve_in_hand = (
            self._lock.locked() and self._fresh(now, max_stale_s) and self._next_read != 0.0
        )
        if now >= self._next_read and not serve_in_hand:
            async with self._lock:
                if time.monotonic() >= self._next_read:
                    await self._refresh(refresh_s)
        if not self._fresh(time.monotonic(), max_stale_s):
            return None
        return self._value

    async def _refresh(self, refresh_s: float) -> None:
        started = time.monotonic()
        try:
            value = await self._read()
        except Exception as e:  # noqa: BLE001 — the last copy serves until it is too old
            self._log.warning("%s: read failed (%s); serving the last copy", self.name, e)
        else:
            self._value, self._read_at = value, started
        # A failed read waits its turn too, so the waiters do not each query in turn.
        done = time.monotonic()
        self._next_read = 0.0 if self._forgot_at >= started else done + refresh_s

    def forget(self) -> None:
        """Make the next call read again, for a change just made here. The copy
        in hand keeps its age, so a failed read serves it as before."""
        self._next_read, self._forgot_at = 0.0, time.monotonic()

    def _reset_for_tests(self, value: T, *, read: bool, refresh_s: float) -> None:
        now = time.monotonic()
        self._value, self._read_at, self._forgot_at = value, (now if read else 0.0), 0.0
        self._next_read = now + refresh_s if read else 0.0
        self._lock = asyncio.Lock()  # a test's event loop is not the last one's
