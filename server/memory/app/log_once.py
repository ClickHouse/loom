"""A warning logged once per key, for a condition read again at every call.

The keys a process remembers stop growing at a cap: past it a condition
still holds, it is just not logged again.
"""

from __future__ import annotations

from collections.abc import Hashable

_CAP = 1000


class LogOnce:
    def __init__(self) -> None:
        self.keys: set[Hashable] = set()

    def first(self, key: Hashable) -> bool:
        """Whether to log `key`: the first time it is seen, while there is room."""
        if key in self.keys or len(self.keys) >= _CAP:
            return False
        self.keys.add(key)
        return True
