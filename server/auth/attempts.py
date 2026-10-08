"""Attempt limits for the unauthenticated auth routes — sign-up and password
sign-in — which run before any token exists to rate-limit by.

Known limit: in-process sliding windows. Each server process counts on its own,
so N replicas or `--workers N` allow N× the limit; a shared counter (the
rate-limit bucket file, or ClickHouse) is the upgrade if that ever matters.
"""

from __future__ import annotations

import time
from typing import Any


class Attempts:
    """At most `limit` recorded attempts per key within `window_s` seconds.
    The key map is bounded: past `max_keys`, keys with no live attempt are
    dropped."""

    def __init__(self, limit: int, window_s: float, *, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: dict[str, list[float]] = {}

    def __len__(self) -> int:
        return len(self._hits)

    def clear(self) -> None:
        self._hits.clear()

    def _live(self, key: str, t: float) -> list[float]:
        live = [x for x in self._hits.get(key, ()) if t - x < self.window_s]
        if live:
            self._hits[key] = live
        else:
            self._hits.pop(key, None)
        return live

    def blocked(self, key: str, *, now: float | None = None) -> bool:
        return len(self._live(key, time.time() if now is None else now)) >= self.limit

    def record(self, key: str, *, now: float | None = None) -> None:
        t = time.time() if now is None else now
        if len(self._hits) >= self.max_keys:
            for k in [k for k, v in self._hits.items() if not v or t - v[-1] >= self.window_s]:
                del self._hits[k]
        self._hits[key] = [*self._live(key, t), t]

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record an attempt unless the key is already at its limit."""
        if self.blocked(key, now=now):
            return False
        self.record(key, now=now)
        return True


def client_address(request: Any) -> str:
    """The address an attempt limit counts. Behind the TLS proxy Loom is
    deployed with, the LAST X-Forwarded-For hop is the one that proxy
    appended; every hop before it is whatever the client sent, so keying on
    the first let a caller mint a fresh bucket per request.
    Known limit: one trusted proxy hop; behind a chain every caller shares the
    outer proxy's bucket (safe, strict) — a trusted-hop count is the upgrade."""
    forwarded: str = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",")]
    if hops[-1]:
        return hops[-1]
    return request.client.host if request.client else "?"
