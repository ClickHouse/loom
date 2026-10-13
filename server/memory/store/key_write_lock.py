"""Per-key lock for conditional memory writes (`if_match_sha256`).

The hash check runs before the embed and the insert, so two writers holding the same
hash could both pass it. Writers of one key are serialized per process, and across
processes by a KeeperMap key in `loom._migration_lock` when the server has a Keeper.
`hold_name` takes the same lock under any name.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from server.core.errors import PreconditionFailedError
from server.core.repo_base import _CHClient, _default_client
from server.core.schema.migrate_lease import (
    LOCK_INSERT,
    LOCK_RELEASE,
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
        async with self.hold_name(name):
            yield

    @asynccontextmanager
    async def hold_name(self, name: str) -> AsyncIterator[None]:
        """Hold `name`; PreconditionFailedError when another holder keeps it past the wait."""
        me = default_holder()
        async with self._process_lock(name):
            if not await self._keeper_acquire(name, me):
                yield
                return
            try:
                yield
            finally:
                await self._keeper_release(name, me)

    @asynccontextmanager
    async def _process_lock(self, name: str) -> AsyncIterator[None]:
        lock, users = self._local.get(name, (asyncio.Lock(), 0))
        self._local[name] = (lock, users + 1)
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=self._wait_s)
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

    async def _keeper_acquire(self, name: str, me: str) -> bool:
        """True once held; False without a usable Keeper."""
        deadline = time.monotonic() + self._wait_s
        poll = self._poll_s
        try:
            if not await self._has_keeper():
                return False
            while not await self._attempt(name, me):
                if time.monotonic() >= deadline:
                    raise _busy()
                await asyncio.sleep(poll)
                poll = min(poll * 1.5, 0.5)
            return True
        except PreconditionFailedError:
            raise
        except Exception as e:  # noqa: BLE001 — Keeper trouble must not fail the write
            degradations.note("memory_key_lock", "process_lock_only", e)
            return False

    async def _keeper_release(self, name: str, me: str) -> None:
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
