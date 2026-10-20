"""Per-key lock for conditional memory writes (`memory.set`'s `if_match_sha256`,
`memory.delete_version`).

The check runs before the insert (for a set, before the embed too), so two writers could
both pass it. Writers of one key are serialized per process, and across processes by a
lease (`server.core.leases`); a Keeper that fails refuses the write (503) rather than lock
this process alone. `lease` holds a lock of any name across hosts for as long as a job runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from server.core import leases
from server.core.errors import PreconditionFailedError
from server.core.leases import LeaseBusy, LeaseLost
from server.core.repo_base import _CHClient, _default_client
from server.observability import degradations


def lock_name(
    org: str, namespace: str, agent: str, user_id: str, session_id: str, memory_key: str
) -> str:
    ident = "\x1f".join((org, namespace, agent, user_id, session_id, memory_key))
    return "memory_key/" + hashlib.sha256(ident.encode()).hexdigest()[:40]


class KeyWriteLocks:
    def __init__(
        self,
        ch: _CHClient | None = None,
        *,
        wait_s: float = 10.0,
        ttl_s: int = 60,
        poll_s: float = 0.05,
    ) -> None:
        self._ch = ch
        self._wait_s = wait_s
        self._ttl_s = ttl_s
        self._poll_s = poll_s
        self._local: dict[str, tuple[asyncio.Lock, int]] = {}

    def __len__(self) -> int:
        return len(self._local)

    @property
    def ch(self) -> _CHClient:
        return self._ch or _default_client()

    @asynccontextmanager
    async def hold(
        self,
        org: str,
        namespace: str,
        agent: str,
        user_id: str,
        session_id: str,
        memory_key: str,
    ) -> AsyncIterator[None]:
        name = lock_name(org, namespace, agent, user_id, session_id, memory_key)
        until = time.monotonic() + self._wait_s
        async with self._process_lock(name, until):
            lease = await self._take(name, until)
            try:
                yield
            finally:
                await lease.release()

    def lease(self, name: str, *, wait_s: float | None = None) -> AbstractAsyncContextManager[Any]:
        """Hold `name` across hosts while the body runs (`leases.cluster_lease`), waiting
        `wait_s` (the instance's wait by default). LeaseBusy when another holder keeps it,
        LeaseLost from the body once it is lost."""
        return leases.cluster_lease(
            name,
            ttl_s=self._ttl_s,
            wait_s=self._wait_s if wait_s is None else wait_s,
            poll_s=self._poll_s,
            poll_max_s=0.5,
            ch=self._ch,
        )

    async def _take(self, name: str, until: float) -> leases.Lease:
        """The key's lease once held. A Keeper that fails is a 503: a per-process lock
        would let another replica's writer pass the same hash check."""
        try:
            wait_s = until - time.monotonic()
            if wait_s <= 0:
                raise _busy()
            lease = await leases.take(
                name,
                ttl_s=self._ttl_s,
                wait_s=wait_s,
                poll_s=self._poll_s,
                poll_max_s=0.5,
                ch=self.ch,
            )
        except PreconditionFailedError:
            raise
        except Exception as e:
            degradations.note("memory_key_lock", "keeper_unavailable", e)
            raise leases.LeaseUnavailable(
                "the conditional write's key lock could not be taken; retry shortly",
                operation="memory_key_lock",
            ) from e
        if lease is None:
            raise _busy()
        return lease

    @asynccontextmanager
    async def _process_lock(self, name: str, until: float) -> AsyncIterator[None]:
        lock, users = self._local.get(name, (asyncio.Lock(), 0))
        self._local[name] = (lock, users + 1)
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=until - time.monotonic())
            except TimeoutError:
                raise _busy() from None
            try:
                yield
            finally:
                lock.release()
        finally:
            lock, users = self._local[name]
            if users == 1:
                del self._local[name]
            else:
                self._local[name] = (lock, users - 1)


def _busy() -> PreconditionFailedError:
    return PreconditionFailedError(
        "another conditional write to this key is still running; re-read and retry"
    )


key_write_locks = KeyWriteLocks()

__all__ = ["KeyWriteLocks", "LeaseBusy", "LeaseLost", "key_write_locks", "lock_name"]
