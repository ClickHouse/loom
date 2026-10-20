"""One process's copy of a small table, read on a timer, for a fail-closed check.

The check is made on every call. The copy reads the whole table when a read is due:
`refresh_s` after the last read ended, or at once after a change made in this process
(`note_change`). While a read runs, a call is served the copy in hand if that copy is young
enough and no change made here waits; otherwise it waits for the read. `get` answers None
when the copy is older than `max_stale_s`, and the caller refuses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable


class PolledCopy[T]:
    def __init__(self, name: str, read: Callable[[], Awaitable[T]], log: logging.Logger) -> None:
        self._name = name
        self._read = read
        self._log = log
        self._value: T | None = None
        # When the copy was read (monotonic, from the start of its query; None =
        # never) and when the next read is due.
        # _changes counts changes made here; _changes_read is its value when the
        # last finished read (failed or not) began.
        self._read_at: float | None = None
        self._next_read = 0.0
        self._changes = 0
        self._changes_read = 0
        self._lock = asyncio.Lock()

    def _fresh(self, now: float, max_stale_s: float) -> bool:
        return self._read_at is not None and now - self._read_at <= max_stale_s

    def _change_waiting(self) -> bool:
        return self._changes != self._changes_read

    def _due(self, now: float) -> bool:
        return now >= self._next_read or self._change_waiting()

    async def get(self, *, refresh_s: float, max_stale_s: float) -> T | None:
        """The copy, read first when a read is due (unless one is running and the
        copy in hand is fresh with no change waiting); None when the copy is older
        than `max_stale_s`, counted from the start of the read that brought it
        (or it was never read)."""
        now = time.monotonic()
        serve_in_hand = (
            self._lock.locked() and self._fresh(now, max_stale_s) and not self._change_waiting()
        )
        if self._due(now) and not serve_in_hand:
            async with self._lock:
                if self._due(time.monotonic()):  # the read we waited for may have done it
                    await self._refresh(refresh_s)
        if not self._fresh(time.monotonic(), max_stale_s):
            return None
        return self._value

    async def _refresh(self, refresh_s: float) -> None:
        started = time.monotonic()
        changes = self._changes
        try:
            value = await self._read()
        except Exception as e:  # noqa: BLE001 — a failed read must not fail the call; the copy's age decides what is served
            self._log.warning("%s: read failed (%s)", self._name, e)
        else:
            self._value = value
            self._read_at = started
        # Reached by a failed read too, so waiters do not each read in turn; a cancelled
        # read skips it, so the next call reads.
        self._changes_read = changes
        self._next_read = time.monotonic() + refresh_s

    def note_change(self) -> None:
        """A change was made here: the next call reads, and calls during a read
        wait for it."""
        self._changes += 1

    def _reset_for_tests(
        self, value: T | None, *, read_age_s: float | None, due_in_s: float
    ) -> None:
        """The copy `value`, read `read_age_s` ago (None: never read), with the
        next read due `due_in_s` from now (0: due now); a fresh lock, since a
        test's event loop is not the last one's."""
        now = time.monotonic()
        self._value = value
        self._read_at = None if read_age_s is None else now - read_age_s
        self._next_read = now + due_in_s
        self._changes = self._changes_read = 0
        self._lock = asyncio.Lock()
