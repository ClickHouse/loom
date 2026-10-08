"""In-process wake-ups for the task inbox long polls.

A pull that finds nothing waits here instead of re-reading ClickHouse in a
tight loop. ``notify`` is called by the writes that can make a wait worth
ending — a create or accept into an inbox, an event on a task a sender
posted — and wakes every waiter on that key in this process. A write that
landed on another web process is caught by the waiter's periodic re-read
(``RECHECK_SECONDS``), so a wake-up is a latency hint and the table stays
the source of truth.
"""

from __future__ import annotations

import asyncio
import time

RECHECK_SECONDS = 2.0

_waiters: dict[str, set[asyncio.Event]] = {}


def inbox_key(org: str, inbox: str) -> str:
    return f"inbox:{org}:{inbox}"


def poster_key(org: str, subject: str) -> str:
    return f"poster:{org}:{subject}"


def notify(*keys: str) -> None:
    for key in keys:
        for ev in _waiters.get(key, ()):
            ev.set()


async def wait(key: str, seconds: float) -> float:
    """Block until ``notify(key)`` or ``seconds`` (capped at RECHECK_SECONDS)
    elapse; returns the seconds actually waited."""
    seconds = max(0.0, min(seconds, RECHECK_SECONDS))
    if seconds <= 0:
        return 0.0
    ev = asyncio.Event()
    bucket = _waiters.setdefault(key, set())
    bucket.add(ev)
    started = time.monotonic()
    try:
        await asyncio.wait_for(ev.wait(), timeout=seconds)
    except TimeoutError:
        pass
    finally:
        bucket.discard(ev)
        if not bucket:
            _waiters.pop(key, None)
    return time.monotonic() - started
