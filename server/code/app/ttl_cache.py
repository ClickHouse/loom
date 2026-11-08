"""Process-local caches with a time-to-live and a size bound.

The code plane keeps small per-process answers (a namespace's `as_of` block, a
mirror's lag, a repository map, a probe's verdict) for seconds to minutes so a
burst of calls reads once. Each is a `TtlCache`: an entry lives `ttl_s` after it
is stored (a `put` may name its own TTL, e.g. a shorter one for a failed read;
a cache whose TTL is a setting takes a callable that reads it at each `put`),
and past `maxsize` entries the least recently used one is dropped, so no cache
grows without bound. A cache of values whose size a repository chooses (a file's text)
also takes `weigh` and `maxweight`: past `maxweight` in total, the least recently used
entries are dropped too.

Every cache registers itself; `clear_all()` empties them all, which is how the
unit suite starts each test from cold caches.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, overload

_CACHES: list[TtlCache[Any, Any]] = []
_MISS = object()


class TtlCache[K, V]:
    """A bounded LRU map whose entries expire `ttl_s` seconds after their `put`."""

    def __init__(
        self,
        ttl_s: float | Callable[[], float],
        *,
        maxsize: int = 1024,
        weigh: Callable[[V], int] | None = None,
        maxweight: int = 0,
    ) -> None:
        self._ttl = ttl_s
        self.maxsize = max(1, int(maxsize))
        self._items: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._weigh = weigh
        self.maxweight = int(maxweight)
        self.weight = 0  # the sum of `weigh` over the entries held
        _CACHES.append(self)

    def _drop(self, key: K) -> None:
        _, value = self._items.pop(key)
        if self._weigh is not None:
            self.weight -= self._weigh(value)

    @property
    def ttl_s(self) -> float:
        """The TTL a `put` gives its entry by default, read now."""
        return float(self._ttl() if callable(self._ttl) else self._ttl)

    @overload
    def get(self, key: K) -> V | None: ...
    @overload
    def get[D](self, key: K, default: D) -> V | D: ...
    def get(self, key: K, default: Any = None) -> Any:
        """The live value under `key`, or `default` (absent or expired). A cache that
        stores None as a value passes a sentinel `default` to tell a miss apart."""
        hit = self._items.get(key)
        if hit is None:
            return default
        if hit[0] <= time.monotonic():
            self._drop(key)
            return default
        self._items.move_to_end(key)
        return hit[1]

    def put(self, key: K, value: V, *, ttl_s: float | None = None) -> None:
        """Store `value` for `ttl_s` seconds (the cache's TTL by default)."""
        life = self.ttl_s if ttl_s is None else float(ttl_s)
        if key in self._items:
            self._drop(key)
        self._items[key] = (time.monotonic() + life, value)
        if self._weigh is not None:
            self.weight += self._weigh(value)
        while len(self._items) > self.maxsize or (
            self.maxweight and self.weight > self.maxweight and len(self._items) > 1
        ):
            self._drop(next(iter(self._items)))

    def pop(self, key: K) -> None:
        if key in self._items:
            self._drop(key)

    def clear(self) -> None:
        self._items.clear()
        self.weight = 0

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return self.get(key, _MISS) is not _MISS  # type: ignore[arg-type]  # any key misses


def clear_all() -> None:
    """Empty every registered cache."""
    for cache in _CACHES:
        cache.clear()


__all__ = ["TtlCache", "clear_all"]
