"""Per-key lock for conditional memory writes (`if_match_sha256`).

The hash check runs before the embed and the insert, so two writers holding the same
hash could both pass it. A process-local lock serializes writers in one process; a
KeeperMap key in `loom._migration_lock` serializes them across processes when the
server has a Keeper.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from server.core.errors import PreconditionFailedError
from server.core.schema.migrate_lease import (
    _LOCK_INSERT,
    _LOCK_RELEASE,
    _LOCK_TAKEOVER,
    LOCK_TABLE,
    default_holder,
)

log = logging.getLogger(__name__)

TTL_S = 60
WAIT_S = 10.0
_POLL_S, _POLL_MAX_S = 0.05, 0.5

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


def _timed_out() -> PreconditionFailedError:
    return PreconditionFailedError(
        "another conditional write to this key is still running; re-read and retry"
    )


class _LocalLocks:
    """asyncio locks by name, dropped when the last user leaves."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(name, (asyncio.Lock(), 0))
        self._locks[name] = (lock, users + 1)
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=WAIT_S)
            except TimeoutError:
                raise _timed_out() from None
            try:
                yield
            finally:
                lock.release()
        finally:
            lock, users = self._locks[name]
            if users == 1:
                del self._locks[name]
            else:
                self._locks[name] = (lock, users - 1)

    def __len__(self) -> int:
        return len(self._locks)


class _KeeperLock:
    def __init__(self, ch: Any, name: str) -> None:
        self._ch = ch
        self._name = name
        self._me = default_holder()

    async def acquire(self) -> bool:
        """False if Keeper failed; raises PreconditionFailedError after WAIT_S."""
        deadline = time.monotonic() + WAIT_S
        poll = _POLL_S
        while True:
            try:
                if await self._attempt():
                    return True
            except Exception as e:  # noqa: BLE001 — Keeper trouble must not fail the write
                log.warning("memory key lock %s: Keeper refused: %s", self._name, e)
                return False
            if time.monotonic() >= deadline:
                raise _timed_out()
            await asyncio.sleep(poll)
            poll = min(poll * 1.5, _POLL_MAX_S)

    async def release(self) -> None:
        try:
            await self._ch.execute(_LOCK_RELEASE, params={"name": self._name, "me": self._me})
        except Exception as e:  # noqa: BLE001 — the key expires after TTL_S
            log.warning("memory key lock %s: release failed: %s", self._name, e)

    async def _attempt(self) -> bool:
        params = {"name": self._name, "me": self._me, "ttl": TTL_S, "commit": ""}
        try:
            await self._ch.execute(_LOCK_INSERT, params=params)
            return True
        except Exception as e:
            if "Node exists" not in str(e):
                raise
        holder = await self._holder()
        if holder is None:
            return False
        name, stale = holder
        if name == self._me:
            return True
        if not stale:
            return False
        try:
            await self._ch.execute(_LOCK_TAKEOVER, params={**params, "old": name})
        except Exception as e:
            if "Bad version" not in str(e) and "KEEPER_EXCEPTION" not in str(e):
                raise
        holder = await self._holder()
        return holder is not None and holder[0] == self._me

    async def _holder(self) -> tuple[str, bool] | None:
        row = await self._ch.query_one(_HOLDER_SQL, params={"name": self._name})
        return None if row is None else (str(row["holder"]), bool(row["stale"]))


_local = _LocalLocks()
_keeper: bool | None = None


async def _keeper_available(ch: Any) -> bool:
    global _keeper
    if _keeper is None:
        try:
            row = await ch.query_one(_ENGINE_SQL, params={"table": LOCK_TABLE.split(".")[1]})
        except Exception as e:  # noqa: BLE001 — probed again on the next write
            log.warning("memory key lock: lock table probe failed: %s", e)
            return False
        _keeper = bool(row and row.get("engine") == "KeeperMap")
    return _keeper


@asynccontextmanager
async def key_write_lock(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    ch: Any | None = None,
) -> AsyncIterator[str]:
    from server.core.ch import get_client

    name = lock_name(org, namespace, agent, user_id, session_id, memory_key)
    async with _local.hold(name):
        client = ch or get_client()
        keeper = _KeeperLock(client, name) if await _keeper_available(client) else None
        if keeper is None or not await keeper.acquire():
            yield name
            return
        try:
            yield name
        finally:
            await keeper.release()


def _reset_for_tests() -> None:
    global _keeper, _local
    _keeper = None
    _local = _LocalLocks()
