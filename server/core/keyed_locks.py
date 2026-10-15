"""One asyncio lock per key, kept only while some caller holds or awaits it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager


class KeyedLocks:
    def __init__(self) -> None:
        # key -> (lock, callers holding or awaiting it); the last one out drops it.
        self._locks: dict[Hashable, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        lock, users = self._locks.get(key) or (asyncio.Lock(), 0)
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    def __len__(self) -> int:
        return len(self._locks)

    def clear(self) -> None:
        self._locks.clear()
