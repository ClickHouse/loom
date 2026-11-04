"""Deployment-wide leases on ClickHouse: one holder of a name at a time, across
every Loom process.

A lease is one key of the KeeperMap table `loom._migration_lock` under
`keeper_map_strict_mode = 1`: of two INSERTs of a key one lands, and an UPDATE
checks the znode version, so of two takers of an expired key one wins. A contender
reads the key first and inserts only when there is none (`expect_free`: inserts
first, for a name almost never held). Loom requires a Keeper
(`server.core.schema.migrate_lease.KeeperRequired`): ClickHouse offers no atomic
arbitration without one.

Expiry is judged on the ClickHouse server's clock. A holder renews every sixth of
the TTL (a twelfth after a failed renewal) and counts the lease lost half the TTL
after the last renewal it saw land, before another process can take it over. There
are no fencing tokens: a statement already sent still lands.

`cluster_lease` holds a name for a block and cancels the block once the lease is
lost; `take` returns a `Lease` to release yourself. `holding` is a claim: a
check-then-insert (a sign-up's "is the name free", an OAuth code's "not consumed
yet") runs under it, so two replicas inside ClickHouse's visibility lag cannot both
pass the check. The boot migrator and the
supervisor's lease thread block, and use `server.core.schema.migrate_lease`, which
runs the same statements on a synchronous client.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from server.core.background import TaskSet
from server.core.errors import BackendUnavailableError

log = logging.getLogger("loom.leases")

LOCK_TABLE = "loom._migration_lock"

ENGINE_SQL = (
    "SELECT engine FROM system.tables WHERE database = currentDatabase() AND name = {table:String}"
)

_STRICT = "SETTINGS keeper_map_strict_mode = 1"
LOCK_HOLDER = (
    "SELECT holder, acquired_at, ttl_s, (renewed_at + toIntervalSecond(ttl_s)) < now64(3) AS stale "
    f"FROM {LOCK_TABLE} WHERE name = {{name:String}}"
)
LOCK_INSERT = (
    f"INSERT INTO {LOCK_TABLE} (name, holder, acquired_at, renewed_at, ttl_s, build_commit) "
    f"{_STRICT}, async_insert = 0 "
    "VALUES ({name:String}, {me:String}, now64(3), now64(3), {ttl:UInt32}, {commit:String})"
)
# The staleness test sits in the WHERE, so it is evaluated on the znode version
# the CAS checks: a holder that renewed a moment ago is not taken over.
LOCK_TAKEOVER = (
    f"ALTER TABLE {LOCK_TABLE} UPDATE holder = {{me:String}}, acquired_at = now64(3), "
    "renewed_at = now64(3), ttl_s = {ttl:UInt32}, build_commit = {commit:String} "
    "WHERE name = {name:String} AND holder = {old:String} "
    f"AND (renewed_at + toIntervalSecond(ttl_s)) < now64(3) {_STRICT}"
)
LOCK_RENEW = (
    f"ALTER TABLE {LOCK_TABLE} UPDATE renewed_at = now64(3) "
    f"WHERE name = {{name:String}} AND holder = {{me:String}} {_STRICT}"
)
LOCK_RELEASE = (
    f"ALTER TABLE {LOCK_TABLE} DELETE "
    f"WHERE name = {{name:String}} AND holder = {{me:String}} {_STRICT}"
)


class LeaseBusy(Exception):
    """Another holder keeps the lease."""


class LeaseLost(Exception):
    """The lease was lost while its block ran: another process may hold it now."""


class LeaseUnavailable(BackendUnavailableError):
    """The lease store did not answer: a lease nobody could confirm is not held."""


def default_holder() -> str:
    return f"{socket.gethostname()[:64]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def key_exists(e: BaseException) -> bool:
    return "Node exists" in str(e)


def lost_race(e: BaseException) -> bool:
    return "Bad version" in str(e) or "KEEPER_EXCEPTION" in str(e)


def _client(ch: Any) -> Any:
    if ch is not None:
        return ch
    from server.core.ch import get_client

    return get_client()


class Lease:
    """One holder's lease on `name`, a Keeper key. `may_take_over(holder)` is asked
    before an expired key is taken from `holder`."""

    def __init__(
        self,
        name: str,
        *,
        ttl_s: float,
        ch: Any = None,
        holder: str | None = None,
        may_take_over: Callable[[str], Awaitable[bool]] | None = None,
        expect_free: bool = False,
    ) -> None:
        self.name = name
        self.ttl_s = ttl_s
        self.ch = _client(ch)
        self.holder = holder or default_holder()
        self.may_take_over = may_take_over
        self.expect_free = expect_free
        self.lost = False
        # When the last attempt or renewal that landed was sent (monotonic).
        self._renewed = 0.0
        # Whether a statement that may have written my key was sent.
        self._sent = False
        self._renewer: asyncio.Task[None] | None = None

    @property
    def held(self) -> bool:
        """Still ours: not lost, and renewed within half the TTL. No ClickHouse call."""
        return not self.lost and time.monotonic() - self._renewed < self.ttl_s / 2

    @property
    def renewed_at(self) -> float:
        """When the attempt or renewal that last landed was sent (monotonic): the
        key's TTL runs from no earlier than this."""
        return self._renewed

    async def attempt(self) -> bool:
        """One try at taking the lease."""
        sent = time.monotonic()
        won = await self._keeper_attempt()
        if won:
            self._renewed = sent
        return won

    async def renew(self) -> bool:
        """Re-stamp the lease; False once another holder has it or it expired."""
        sent = time.monotonic()
        ours = await self._keeper_renew()
        if ours:
            self._renewed = sent
        return ours

    async def release(self) -> None:
        """Give the lease up; never raises (an unreleased lease expires by its TTL)."""
        if self._renewer is not None:
            self._renewer.cancel()
        if not self._sent:
            return
        try:
            await self.ch.execute(LOCK_RELEASE, params=self._params())
            self._sent = False
        except Exception as e:  # noqa: BLE001 — the lease expires by its TTL
            log.warning("lease %s: release failed (%s); it expires by its TTL", self.name, e)

    def let_expire(self) -> None:
        """Stop renewing and leave the key to expire by its TTL, judged on the
        server's clock: `release` then deletes nothing. For a holder that cannot
        tell whether its last write will still land, so no other process may
        start on what the lease guards until that write has landed or stopped."""
        if self._renewer is not None:
            self._renewer.cancel()
        self._sent = False

    def keep_renewed(self, on_lost: Callable[[], Any] | None = None) -> None:
        """Renew from a task until `release`; call `on_lost` once the lease is lost."""
        self._renewer = asyncio.create_task(self._renew_loop(on_lost), name=f"lease {self.name}")

    @asynccontextmanager
    async def renewed(self) -> AsyncIterator[Lease]:
        """Run the block with the lease renewed, and release it after. Once the lease
        is lost the block is cancelled and LeaseLost raised."""
        body = asyncio.current_task()
        self.keep_renewed(on_lost=body.cancel if body is not None else None)
        try:
            yield self
        except asyncio.CancelledError:
            if self.lost and body is not None and body.uncancel() == 0:
                raise LeaseLost(self.name) from None
            raise
        finally:
            await self.release()

    async def _renew_loop(self, on_lost: Callable[[], Any] | None) -> None:
        pause = self.ttl_s / 6
        while True:
            deadline = self._renewed + self.ttl_s / 2
            await asyncio.sleep(max(0.0, min(pause, deadline - time.monotonic())))
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                if not await asyncio.wait_for(self.renew(), left):
                    break
                pause = self.ttl_s / 6
            except Exception as e:  # noqa: BLE001 — retried until half the TTL has passed
                log.warning("lease %s: renewal failed (%s)", self.name, e)
                pause = self.ttl_s / 12
        self.lost = True
        log.error("lease %s lost: another process may hold it now", self.name)
        if on_lost is not None:
            on_lost()

    # -- Keeper key --------------------------------------------------------------

    def _params(self, **extra: Any) -> dict[str, Any]:
        return {
            "name": self.name,
            "me": self.holder,
            "ttl": math.ceil(self.ttl_s),
            "commit": "",
            **extra,
        }

    async def _holder_row(self) -> tuple[str, bool] | None:
        row = await self.ch.query_one(LOCK_HOLDER, {"name": self.name})
        return None if row is None else (str(row["holder"]), bool(row["stale"]))

    async def _keeper_attempt(self) -> bool:
        row = None if self.expect_free else await self._holder_row()
        if row is None:
            self._sent = True
            try:
                await self.ch.execute(LOCK_INSERT, params=self._params())
                return True
            except Exception as e:
                if not key_exists(e):
                    raise
                self._sent = False
            row = await self._holder_row()
            if row is None:  # released between the insert and the read
                return False
        holder, stale = row
        if holder == self.holder:  # an insert that landed, or renewals that lapsed
            return await self._keeper_renew()
        if not stale or (self.may_take_over is not None and not await self.may_take_over(holder)):
            return False
        self._sent = True
        try:
            await self.ch.execute(LOCK_TAKEOVER, params=self._params(old=holder))
        except Exception as e:
            if not lost_race(e):
                raise
        won = await self._holder_row() == (self.holder, False)
        self._sent = won
        if won:
            log.info("lease %s: took over from %s (stopped renewing)", self.name, holder)
        return won

    async def _keeper_renew(self) -> bool:
        await self.ch.execute(LOCK_RENEW, params=self._params())
        return await self._holder_row() == (self.holder, False)


async def take(
    name: str,
    *,
    ttl_s: float,
    wait_s: float = 0.0,
    poll_s: float = 0.1,
    poll_max_s: float = 2.0,
    renew: bool = False,
    ch: Any = None,
    holder: str | None = None,
    may_take_over: Callable[[str], Awaitable[bool]] | None = None,
    expect_free: bool = False,
) -> Lease | None:
    """The lease on `name`, or None while another holder keeps it past `wait_s`.
    `renew` keeps it renewed until `release`. Raises LeaseUnavailable when the store
    cannot answer."""
    until = time.monotonic() + wait_s
    lease: Lease | None = None
    try:
        lease = Lease(
            name,
            ttl_s=ttl_s,
            ch=ch,
            holder=holder,
            may_take_over=may_take_over,
            expect_free=expect_free,
        )
        # No attempt starts after the wait, however late we woke; 0 is one attempt.
        won = await lease.attempt() if wait_s <= 0 or time.monotonic() < until else False
        while not won and (left := until - time.monotonic()) > 0:
            await asyncio.sleep(min(poll_s, left))
            if time.monotonic() >= until:
                break
            poll_s = min(poll_s * 1.5, poll_max_s)
            won = await lease.attempt()
    except BaseException as e:
        if lease is not None:
            await lease.release()  # a failed or cancelled statement may have landed
        if isinstance(e, Exception):
            raise LeaseUnavailable("the lease store did not answer", operation=name) from e
        raise
    if not won:
        await lease.release()  # a key my attempt may have written
        return None
    if renew:
        lease.keep_renewed()
    return lease


@asynccontextmanager
async def cluster_lease(name: str, *, ttl_s: float, **options: Any) -> AsyncIterator[Lease]:
    """Hold `name` for the block, renewed (`options` as for `take`). LeaseBusy when
    another holder keeps it past `wait_s`, LeaseUnavailable when the store cannot
    answer; once the lease is lost the block is cancelled and LeaseLost raised."""
    lease = await take(name, ttl_s=ttl_s, **options)
    if lease is None:
        raise LeaseBusy(name)
    async with lease.renewed():
        yield lease


async def held_by(name: str, holder: str, *, ch: Any = None) -> bool:
    """Whether `holder` holds `name` now, on the store's clock: a third party's
    fence for work a holder hands off. LeaseUnavailable when the store cannot answer."""
    try:
        row = await _client(ch).query_one(LOCK_HOLDER, {"name": name})
    except Exception as e:
        raise LeaseUnavailable(f"lease {name}: {e}") from e
    return row is not None and str(row["holder"]) == holder and not row["stale"]


# Releases run under a shield, kept referenced until they finish.
_releases = TaskSet()


async def release_shielded(lease: Lease) -> None:
    """Release `lease` to its end even when the caller is cancelled, again and again
    (anyio repeats a cancellation): a release cut off would hold the name until its TTL."""
    await asyncio.shield(_releases.spawn(lease.release(), name=f"lease {lease.name} release"))


# The block a claim covers ends within this share of its TTL, each statement within
# `_STATEMENT_SHARE`: a claim is never renewed, so its work must end before another
# process may take it over.
_WORK_SHARE = 0.4
_STATEMENT_SHARE = 0.15


class ClaimExpired(LeaseUnavailable):
    """The work under a claim did not finish well inside its TTL: it was cut off."""


@asynccontextmanager
async def holding(
    name: str, *, ttl_s: float, wait_s: float = 0.0, ch: Any = None
) -> AsyncIterator[bool]:
    """Claim `name` for the block, waiting up to `wait_s`: yields True when this
    caller has it, False when another holder keeps it. LeaseUnavailable when the
    store cannot answer, ClaimExpired when a held block runs past `_WORK_SHARE` of
    the TTL."""
    from server.core.ch import statement_timeout

    lease = await take(name, ttl_s=ttl_s, wait_s=wait_s, ch=ch)
    if lease is None:
        yield False
        return
    # From when the winning attempt was sent, not when its reply came: a reply held
    # up past the TTL must not open the block beside the claim's next holder.
    left = lease.renewed_at + ttl_s * _WORK_SHARE - time.monotonic()
    try:
        if left <= 0:
            raise TimeoutError
        async with asyncio.timeout(left):
            with statement_timeout(max(1.0, ttl_s * _STATEMENT_SHARE)):
                yield True
    except TimeoutError as e:
        raise ClaimExpired(
            f"the work under claim {name} did not finish in time", operation=name
        ) from e
    finally:
        await release_shielded(lease)


# KeeperMap keys their writer deletes, unless it died first: per table, the delete of
# the dead ones.
DEAD_KEYS = {
    "_migration_lock": (
        f"ALTER TABLE {LOCK_TABLE} DELETE "
        f"WHERE (renewed_at + toIntervalSecond(ttl_s)) < now64(3) - toIntervalHour(1) {_STRICT}"
    ),
    # Slot keys of projections that died; a namespace's write key stays (it is raised
    # by UPDATE alone once a process has seen it).
    "graph_projections": (
        "ALTER TABLE loom.graph_projections DELETE "
        f"WHERE finished = 0 AND started_at < now64(3) - toIntervalDay(1) {_STRICT}"
    ),
}


async def sweep_dead_keys(ch: Any = None) -> int:
    """Delete the dead keys of every KeeperMap table in `DEAD_KEYS`; the number of
    tables swept. A key written meanwhile fails the delete ("Bad version"): the next
    sweep retries."""
    ch = _client(ch)
    for sql in DEAD_KEYS.values():
        await ch.execute(sql)
    return len(DEAD_KEYS)


__all__ = [
    "LOCK_TABLE",
    "Lease",
    "LeaseBusy",
    "LeaseLost",
    "LeaseUnavailable",
    "cluster_lease",
    "default_holder",
    "held_by",
    "holding",
    "key_exists",
    "lost_race",
    "sweep_dead_keys",
    "take",
]
