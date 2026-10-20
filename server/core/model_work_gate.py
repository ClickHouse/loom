"""Process-local admission and non-cancelling drain for model transitions.

This is one replica's gate, not a distributed lock. A rebuild coordinator must
close every participating replica, drain deferred writes and verify their
acknowledgements before touching vector storage.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from server.core import readiness


class ModelChangeInProgress(RuntimeError):
    """New data-plane work must retry after the model transition."""


class ModelWorkGate:
    """Used on one event loop; admission never awaits between checking and counting."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._active = 0
        self._open = asyncio.Event()
        self._open.set()
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def active(self) -> int:
        return self._active

    @property
    def closed(self) -> bool:
        return self._token is not None

    def close(self, token: str) -> None:
        """Stop admission immediately; only the same transition can retry closing."""
        if not token or self._token not in (None, token):
            raise ValueError("Model transition token does not own admission")
        self._token = token
        self._open.clear()

    async def wait_drained(self, token: str, *, timeout_s: float) -> bool:
        """A timeout leaves admission closed and never cancels admitted work."""
        self._require_owner(token)
        if timeout_s < 0:
            raise ValueError("Drain timeout must be nonnegative")
        if self._active:
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=timeout_s)
            except TimeoutError:
                return False
        self._require_owner(token)
        return self._active == 0

    def resume(self, token: str) -> None:
        """Reopen only a drained gate owned by the committed/aborted transition."""
        self._require_owner(token)
        if self._active:
            raise RuntimeError("Model work has not drained")
        self._token = None
        self._open.set()

    def transfer(self, old: str, new: str) -> None:
        """Hand admission, closed by `old`, to the transition `new` without opening it."""
        self._require_owner(old)
        if not new:
            raise ValueError("Model transition token does not own admission")
        self._token = new

    def _require_owner(self, token: str) -> None:
        if not token or self._token != token:
            raise ValueError("Model transition token does not own admission")

    @asynccontextmanager
    async def admit(self, *, wait: bool = False) -> AsyncIterator[None]:
        """HTTP rejects new work; supervised workers wait without spending retries."""
        while self.closed:
            if not wait:
                raise ModelChangeInProgress("Model transition in progress")
            await self._open.wait()
        self._active += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._active -= 1
            if not self._active:
                self._idle.set()


_gate = ModelWorkGate()


def get_model_work_gate() -> ModelWorkGate:
    return _gate


def readiness_check() -> readiness.CheckResult:
    return readiness.result(not _gate.closed, "model change in progress")
