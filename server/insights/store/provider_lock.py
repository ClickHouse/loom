"""Cross-process serialization of short operator-configuration writes."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from server.core.provider_process import process_identity, stopped_process
from server.core.schema.migrate import client
from server.core.schema.migrate_lease import KeeperLock, LeaseConfig


class ProcessKeeperLock(KeeperLock):
    """Never steal a lock from a process merely because its heartbeat expired.

    Recovery requires a kernel death proof on the shared persistent volume AND
    no residual ClickHouse work. An unreachable remote owner blocks recovery.
    """

    label = "provider configuration lock"

    def _storage_idle(self) -> bool:
        checks = (
            "SELECT count() FROM system.processes WHERE query_id != currentQueryID() "
            "AND (current_database = 'loom' OR positionCaseInsensitive(query, 'loom.') > 0)",
            "SELECT count() FROM system.mutations WHERE database = 'loom' AND NOT is_done",
            "SELECT count() FROM system.asynchronous_inserts WHERE database = 'loom'",
        )
        for sql in checks:
            rows = self.ch.query(sql).result_rows
            if len(rows) != 1 or len(rows[0]) != 1:
                return False
            if type(rows[0][0]) is not int or rows[0][0] != 0:
                return False
        return True

    def _attempt(self) -> tuple[bool, Any]:
        if self._try_insert():
            return True, None
        row = self._holder_row()
        if row is None:
            return False, None
        with stopped_process(row[0].split("/", 1)[0]) as stopped:
            if stopped and self._storage_idle():
                self.ch.command(
                    "ALTER TABLE loom._migration_lock DELETE WHERE name = {name:String} "
                    "AND holder = {old:String} SETTINGS keeper_map_strict_mode = 1",
                    parameters={"name": self.lock_name, "old": row[0]},
                )
        return False, row


class ConfigurationLockUnavailable(RuntimeError):
    """The shared configuration lock could not be acquired."""


async def _finish_thread[T](operation: Callable[[], T]) -> T:
    """Cancellation must not let cleanup race an operation on the same client."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve failures too, so the thread never leaves an unobserved task.
        try:
            task.result()
        finally:
            raise asyncio.CancelledError
    return task.result()


@asynccontextmanager
async def configuration_lock(
    wait_s: float = 5, out: Callable[[str], None] = lambda _: None
) -> AsyncIterator[KeeperLock]:
    """Use an independent key in the existing Keeper lock table.

    Never fall back to a process-local lock: two containers could then overwrite
    one another's keys after both passed an expected-version check. Validation is
    performed before entering this short critical section.
    """
    # Keep ownership even when cancellation arrives while client() is connecting.
    # _finish_thread waits for it, then this finally closes the resulting client.
    clients = []

    def connect() -> Any:
        ch = client()
        clients.append(ch)
        return ch

    lock = None
    try:
        ch = await _finish_thread(connect)
        lock = ProcessKeeperLock(
            ch,
            holder=f"{process_identity()}/{uuid4()}",
            lock_name="provider_configuration",
            cfg=LeaseConfig(ttl_s=300, wait_s=wait_s),
            out=out,
        )
        outcome = await _finish_thread(lock.acquire)
        if outcome != "held":
            raise ConfigurationLockUnavailable("provider_configuration_lock_unavailable")
        yield lock
    finally:
        try:
            if lock is not None:
                await _finish_thread(lock.release)
        finally:
            for ch in clients:
                await _finish_thread(ch.close)
