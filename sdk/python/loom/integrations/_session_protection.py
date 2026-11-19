"""Retained application-owned controls for automatic framework callbacks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from loom.session_protection import (
    SessionProtection,
    automatic_for,
    observation_scope,
    response_roots,
)


class _CallbackProtection:
    def __init__(self, bound: SessionProtection | None) -> None:
        self._roots = response_roots(bound)
        self._lock = Lock()

    def remember(self, bound: SessionProtection | None) -> None:
        # A later callback or client rebinding cannot remove an earlier contributor.
        roots = response_roots(bound)
        with self._lock:
            self._roots = tuple(dict.fromkeys((*self._roots, *roots)))

    @contextmanager
    def automatic(self, bound: SessionProtection | None) -> Iterator[None]:
        self.remember(bound)
        with automatic_for(self._roots):
            yield

    @contextmanager
    def observing(self, bound: SessionProtection | None) -> Iterator[None]:
        self.remember(bound)
        with observation_scope(self._roots):
            yield

    @property
    def protected(self) -> bool:
        return any(root.protected for root in self._roots)
