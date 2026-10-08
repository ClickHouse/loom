"""Process-local caches with a time-to-live and a size bound.

The code plane keeps small per-process answers (a namespace's `as_of` block, a
mirror's lag, a repository map, a probe's verdict) for seconds to minutes so a
burst of calls reads once. Each is a `TtlCache`: an entry lives `ttl_s` after it
is stored (a `put` may name its own TTL, e.g. a shorter one for a failed read),
and past `maxsize` entries the least recently used one is dropped, so no cache
grows without bound.

Every cache registers itself; `clear_all()` empties them all, which is how the
unit suite starts each test from cold caches.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, overload

_CACHES: list[TtlCache[Any, Any]] = []
_MISS = object()


class TtlCache[K, V]:
    """A bounded LRU map whose entries expire `ttl_s` seconds after their `put`."""

    def __init__(self, ttl_s: float, *, maxsize: int = 1024) -> None:
        self.ttl_s = float(ttl_s)
        self.maxsize = max(1, int(maxsize))
        self._items: OrderedDict[K, tuple[float, V]] = OrderedDict()
        _CACHES.append(self)

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
            del self._items[key]
            return default
        self._items.move_to_end(key)
        return hit[1]

    def put(self, key: K, value: V, *, ttl_s: float | None = None) -> None:
        """Store `value` for `ttl_s` seconds (the cache's TTL by default)."""
        life = self.ttl_s if ttl_s is None else float(ttl_s)
        self._items[key] = (time.monotonic() + life, value)
        self._items.move_to_end(key)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def pop(self, key: K) -> None:
        self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return self.get(key, _MISS) is not _MISS  # type: ignore[arg-type]  # any key misses


def clear_all() -> None:
    """Empty every registered cache."""
    for cache in _CACHES:
        cache.clear()


__all__ = ["TtlCache", "clear_all"]
