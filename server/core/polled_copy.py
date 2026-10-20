"""One process's copy of a small table a security path reads on every call.

The model-call switch (`server.llm.customer`) and the extraction policy
(`server.memory.app.extraction_policy`) each keep one: the whole table is
read at most every `refresh_s`, a change made in this process asks for the
next read at once (`forget`), and a copy older than `max_stale_s` is not
trusted, so its callers fail closed.

A read is due again only once the read in progress is over: a call made
meanwhile waits on the lock for it rather than finding the copy it replaces
too old, and a cancelled read leaves the next call to read. A change made
here while a read ran may postdate what it read, so the next call reads again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class PolledCopy[T]:
    def __init__(self, name: str, empty: T, read: Callable[[], Awaitable[T]]) -> None:
        self.name = name
        self.value = empty
        self._read = read
        # When the copy was read (monotonic; 0.0 = never), when the next read is
        # due, and when a change here last asked for one.
        self.read_at = 0.0
        self.next_read = 0.0
        self.forgot_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, *, refresh_s: float, max_stale_s: float) -> T | None:
        """The copy, read first when a read is due; None when it is too old to
        trust (never read, or every read since `max_stale_s` failed)."""
        if time.monotonic() >= self.next_read:
            async with self._lock:
                if time.monotonic() >= self.next_read:
                    await self._refresh(refresh_s)
        if not self.read_at or time.monotonic() - self.read_at > max_stale_s:
            return None
        return self.value

    async def _refresh(self, refresh_s: float) -> None:
        started = time.monotonic()
        try:
            value = await self._read()
        except Exception as e:  # noqa: BLE001 — the last copy serves until it is too old
            log.warning("%s: read failed (%s); serving the last copy", self.name, e)
        else:
            self.value, self.read_at = value, time.monotonic()
        # A failed read waits its turn too, so the waiters do not each query in turn.
        self.next_read = 0.0 if self.forgot_at >= started else started + refresh_s

    def forget(self) -> None:
        """Make the next call read again, for a change just made here. The copy
        in hand keeps its age, so a failed read serves it as before."""
        self.next_read, self.forgot_at = 0.0, time.monotonic()

    def reset_for_tests(self, value: T, *, read: bool, refresh_s: float) -> None:
        now = time.monotonic()
        self.value, self.read_at, self.forgot_at = value, (now if read else 0.0), 0.0
        self.next_read = now + refresh_s if read else 0.0
