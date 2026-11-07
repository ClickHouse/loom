"""Work a background job set aside until its hold ends.

A job whose call for one identity is held (`customer.GROUP_HOLDS`: the inference
gateway refused it or asked it to wait, or what names the identity could not be
read) leaves that work and goes on with other work. It keeps the work's key here
until the hold ends, so its next selections skip the work instead of picking it
again, ahead of other identities' work, only to be held again. The conflict resolver
also sets aside a row whose flagged peer is behind its delivery, for
`LOOM_PG_DELIVERY_LEASE_SECONDS`.
"""

from __future__ import annotations

import time
import weakref
from collections.abc import Hashable, Iterable
from typing import Any

from server.core.errors import ModelCallRefused

# How long work is left when what names its identity could not be read, or its org was
# switched off since the job picked it: the next pass may well find it readable.
UNREAD_HOLD_S = 30.0  # not a setting: the identity reads retry within a minute anyway
_MAX = 10_000  # not a setting: bounds memory; work dropped from it is only tried again

# Every registry, for the test suite to empty between tests.
_every: weakref.WeakSet[Any] = weakref.WeakSet()


def hold_seconds(e: BaseException) -> float:
    """How long work held by `e` is left: a gateway's refusal or wait says (its
    cooldown or Retry-After, 0 for none); any other hold, UNREAD_HOLD_S."""
    return e.hold_s if isinstance(e, ModelCallRefused) else UNREAD_HOLD_S


class Held[K: Hashable]:
    """Keys of held work, each until its hold ends (time.monotonic)."""

    def __init__(self) -> None:
        self._until: dict[K, float] = {}
        _every.add(self)

    def hold(self, keys: Iterable[K], seconds: float) -> None:
        """Leave `keys` for `seconds`; 0 or less leaves nothing."""
        if seconds <= 0:
            return
        until = time.monotonic() + seconds
        for key in keys:
            if key not in self._until and len(self._until) >= _MAX:
                self._prune()
                if len(self._until) >= _MAX:
                    self._until.clear()
            self._until[key] = max(until, self._until.get(key, 0.0))

    def current(self) -> frozenset[K]:
        """The keys still held."""
        self._prune()
        return frozenset(self._until)

    def left(self, key: K) -> float:
        """Seconds left of `key`'s hold; 0 when it is not held."""
        return max(0.0, self._until.get(key, 0.0) - time.monotonic())

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, until in self._until.items() if until <= now]:
            del self._until[key]


def _reset_for_tests() -> None:
    for held in list(_every):
        held._until.clear()
