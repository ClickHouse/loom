"""Per-key lock for conditional memory writes (`if_match_sha256`).

The hash check runs before the embed and the insert, so two writers holding the same
hash could both pass it. Writers of one key are serialized per process, and across
processes by a KeeperMap key in `loom._migration_lock` when the server has a Keeper.
`lease` holds a lock of any name across hosts for as long as a job runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from server.core.errors import PreconditionFailedError
from server.core.repo_base import _CHClient, _default_client
from server.core.schema.migrate_lease import (
    LOCK_INSERT,
    LOCK_RELEASE,
    LOCK_RENEW,
    LOCK_TABLE,
    LOCK_TAKEOVER,
    default_holder,
    key_exists,
    lost_race,
)
from server.observability import degradations

_HOLDER_SQL = (
    "SELECT holder, (renewed_at + toIntervalSecond(ttl_s)) < now64(3) AS stale "
    f"FROM {LOCK_TABLE} WHERE name = {{name:String}}"
)
_ENGINE_SQL = "SELECT engine FROM system.tables WHERE database = 'loom' AND name = {table:String}"


def lock_name(
    org: str, namespace: str, agent: str, user_id: str, session_id: str, memory_key: str
) -> str:
    ident = "\x1f".join((org, namespace, agent, user_id, session_id, memory_key))
    return "memory_key/" + hashlib.sha256(ident.encode()).hexdigest()[:40]


class LeaseBusy(Exception):
    """Another holder keeps the lease."""


class LeaseLost(Exception):
    """The lease could not be confirmed ours before it expired, or another holder has it."""


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
        self._keeper: bool | None = None

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
        me, until = default_holder(), time.monotonic() + self._wait_s
        async with self._process_lock(name, until):
            try:  # released however it ends: a cancelled insert may have landed
                await self._keeper_acquire(name, me, until)
                yield
            finally:
                await self._keeper_release(name, me)

    @asynccontextmanager
    async def lease(self, name: str, *, wait_s: float | None = None) -> AsyncIterator[None]:
        """Hold `name` across hosts while the body runs, renewed every third of the TTL.
        LeaseBusy when it is held, in this process or by another host, past `wait_s` (the
        instance's wait by default) or Keeper fails; LeaseLost, from the body, once the
        lease cannot be confirmed ours before it expires. Without Keeper it is held in
        this process."""
        me, lost = default_holder(), asyncio.Event()
        body = asyncio.current_task()
        until = time.monotonic() + (self._wait_s if wait_s is None else wait_s)
        async with AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(self._process_lock(name, until))
                # released however it ends: a failed or cancelled insert may have landed
                stack.push_async_callback(self._keeper_release, name, me)
                stamped = await self._acquire(name, me, until)
            except PreconditionFailedError:
                raise LeaseBusy(name) from None
            except Exception as e:  # a lease Keeper cannot confirm is not held
                degradations.note("memory_key_lock", "lease_unconfirmed", e)
                raise LeaseBusy(name) from e
            if stamped is not None and body is not None:
                deadline = stamped + self._ttl_s
                renewing = asyncio.create_task(self._renew(name, me, body, lost, deadline))
                stack.callback(renewing.cancel)
            try:
                yield
            except asyncio.CancelledError:
                if lost.is_set() and body is not None and body.uncancel() == 0:
                    raise LeaseLost(name) from None
                raise

    async def _renew(
        self, name: str, me: str, body: asyncio.Task[Any], lost: asyncio.Event, deadline: float
    ) -> None:
        """Re-stamp the lease every third of its TTL; cancel `body` once it is lost.
        `deadline` is when the last confirmed stamp expires: a renewal that fails is
        retried, sooner, until then, and the lease is lost there if none is confirmed."""
        wait = self._ttl_s / 3
        while True:
            await asyncio.sleep(max(0.0, min(wait, deadline - time.monotonic())))
            started = time.monotonic()
            if started >= deadline:
                break
            try:
                ours = await asyncio.wait_for(self._renewed(name, me), deadline - started)
            except Exception as e:  # noqa: BLE001 — retried while the lease lasts
                degradations.note("memory_key_lock", "renew_failed", e)
                wait = max(self._poll_s, (deadline - time.monotonic()) / 4)
                continue
            if not ours:
                break
            deadline, wait = started + self._ttl_s, self._ttl_s / 3
        lost.set()
        body.cancel()

    async def _renewed(self, name: str, me: str) -> bool:
        """Re-stamp the lease; True when the store then shows it ours and unexpired."""
        await self.ch.execute(LOCK_RENEW, params={"name": name, "me": me})
        return await self._holder(name) == (me, False)

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

    async def _keeper_acquire(self, name: str, me: str, until: float) -> bool:
        """True once held; False without a usable Keeper."""
        try:
            return await self._acquire(name, me, until) is not None
        except PreconditionFailedError:
            raise
        except Exception as e:  # noqa: BLE001 — Keeper trouble must not fail the write
            degradations.note("memory_key_lock", "process_lock_only", e)
            return False

    async def _acquire(self, name: str, me: str, until: float) -> float | None:
        """When the attempt that took the lock began (its stamp is no older), once held;
        None when the server has no Keeper. Raises PreconditionFailedError at `until`
        (monotonic), and what a Keeper read raises."""
        poll = self._poll_s
        if not await self._has_keeper():
            return None
        while True:
            started = time.monotonic()
            if started >= until:  # no attempt starts after the wait, however late we woke
                raise _busy()
            if await self._attempt(name, me):
                return started
            await asyncio.sleep(min(poll, until - time.monotonic()))
            poll = min(poll * 1.5, 0.5)

    async def _keeper_release(self, name: str, me: str) -> None:
        if not self._keeper:
            return  # no insert was sent: no Keeper, or none known yet
        try:
            await self.ch.execute(LOCK_RELEASE, params={"name": name, "me": me})
        except Exception as e:  # noqa: BLE001 — the key expires after its TTL
            degradations.note("memory_key_lock", "release_left_to_ttl", e)

    async def _has_keeper(self) -> bool:
        if self._keeper is None:
            row = await self.ch.query_one(_ENGINE_SQL, params={"table": LOCK_TABLE.split(".")[1]})
            self._keeper = bool(row and row.get("engine") == "KeeperMap")
        return self._keeper

    async def _attempt(self, name: str, me: str) -> bool:
        params = {"name": name, "me": me, "ttl": self._ttl_s, "commit": ""}
        try:
            await self.ch.execute(LOCK_INSERT, params=params)
            return True
        except Exception as e:
            if not key_exists(e):
                raise
        holder = await self._holder(name)
        if holder is None or holder[0] == me:
            return holder is not None
        if not holder[1]:
            return False
        try:
            await self.ch.execute(LOCK_TAKEOVER, params={**params, "old": holder[0]})
        except Exception as e:
            if not lost_race(e):
                raise
        holder = await self._holder(name)
        return holder is not None and holder[0] == me

    async def _holder(self, name: str) -> tuple[str, bool] | None:
        row = await self.ch.query_one(_HOLDER_SQL, params={"name": name})
        return None if row is None else (str(row["holder"]), bool(row["stale"]))


def _busy() -> PreconditionFailedError:
    return PreconditionFailedError(
        "another conditional write to this key is still running; re-read and retry"
    )


key_write_locks = KeyWriteLocks()
