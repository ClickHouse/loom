"""One holder at a time per key, claimed in a KeeperMap table.

`claim` inserts the key under keeper_map_strict_mode, where Keeper refuses a
second insert, so of two claimants one holds it. A claim older than `stale_s`
was left by a holder that died, and is taken over with Keeper's
compare-and-swap, so of two takers one wins. A server without a Keeper has no
table: `claim` answers UNCLAIMED and the caller decides what to do without it.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from collections.abc import Coroutine
from typing import Any

from server.core.background import TaskSet
from server.core.ch import statement_timeout
from server.core.schema.migrate_lease import is_keeper_table, key_exists, lost_race

log = logging.getLogger(__name__)

_STRICT = "SETTINGS keeper_map_strict_mode = 1"
# A no is asked again after this, so a server that booted before the migration
# created the table starts claiming without a restart.
_ABSENT_RECHECK_S = 60.0

# Release tasks run under a shield, kept referenced until they finish.
_releases = TaskSet()


async def shielded(release: Coroutine[Any, Any, None], *, name: str) -> None:
    """Run a release to its end even when the request awaiting it is
    cancelled, again and again (anyio repeats a cancellation): the task is held
    in `_releases` and the wait for it is shielded."""
    await asyncio.shield(_releases.spawn(release, name=name))


async def drain(timeout_s: float = 5.0) -> int:
    """Wait for the shielded writes still running (at most `timeout_s`);
    returns how many were. Shutdown calls it before the client disconnects:
    a request cancelled there leaves its write to finish here."""
    live = [t for t in _releases if not t.done()]
    if live:
        await asyncio.wait(live, timeout=timeout_s)
    return len(live)


class Claim(enum.Enum):
    HELD = enum.auto()  # this caller holds the key
    TAKEN = enum.auto()  # another holder has it
    UNCLAIMED = enum.auto()  # no claims table here


class KeeperClaims:
    """The claims kept in `loom.<table>`, a KeeperMap table of
    (claim_key, holder, claimed_at)."""

    def __init__(
        self, table: str, *, stale_s: int, claim_timeout_s: float, release_timeout_s: float
    ) -> None:
        self.table = table
        self.stale_s = stale_s
        self.claim_timeout_s = claim_timeout_s
        self.release_timeout_s = release_timeout_s
        t = f"loom.{table}"
        self.insert_sql = (
            f"INSERT INTO {t} (claim_key, holder, claimed_at) {_STRICT} "
            "VALUES ({k:String}, {me:String}, now64(3))"
        )
        self.holder_sql = (
            "SELECT holder, claimed_at < now64(3) - toIntervalSecond({stale:UInt32}) AS stale "
            f"FROM {t} WHERE claim_key = {{k:String}}"
        )
        # The staleness test sits in the WHERE, so it is evaluated on the znode version
        # Keeper's compare-and-swap checks: of two takers one wins, the other loses the race.
        self.take_over_sql = (
            f"ALTER TABLE {t} UPDATE holder = {{me:String}}, claimed_at = now64(3) "
            "WHERE claim_key = {k:String} "
            f"AND claimed_at < now64(3) - toIntervalSecond({{stale:UInt32}}) {_STRICT}"
        )
        self.sweep_sql = (
            f"ALTER TABLE {t} DELETE "
            f"WHERE claimed_at < now64(3) - toIntervalSecond({{stale:UInt32}}) {_STRICT}"
        )
        self.release_sql = (
            f"ALTER TABLE {t} DELETE "
            f"WHERE claim_key = {{k:String}} AND holder = {{me:String}} {_STRICT}"
        )
        self.table_sql = (
            f"SELECT engine FROM system.tables WHERE database = 'loom' AND name = '{table}'"
        )
        # Whether the table exists here: None until asked.
        self.atomic: bool | None = None
        self.absent_until = 0.0

    async def probe(self, ch: Any) -> None:
        """At boot: find whether the table exists, and sweep the stale claims a
        holder kept or failed to release."""
        try:
            available = await self.available(ch)
        except Exception as e:  # noqa: BLE001 — a failed probe leaves it to the first claim
            log.warning("%s: could not read system.tables: %s", self.table, e)
            return
        if available:
            try:
                with statement_timeout(5):
                    await ch.execute(self.sweep_sql, params={"stale": self.stale_s})
            except Exception as e:  # noqa: BLE001 — a later boot sweeps; a stale claim is taken over meanwhile
                log.warning("%s: sweep of stale claims failed: %s", self.table, e)

    async def claim(self, ch: Any, key: str) -> tuple[Claim, str]:
        """Claim `key`: (outcome, holder id to release with). Raises when Keeper
        cannot answer, so the caller refuses retryably rather than run unclaimed."""
        me = uuid.uuid4().hex
        attempted = False
        try:
            # One wall-clock bound over the whole acquisition: a lost reply to an
            # insert that landed, then reconnects, would otherwise age the claim
            # toward `stale_s` before the holder even starts.
            async with asyncio.timeout(self.claim_timeout_s):
                if not await self.available(ch):
                    return Claim.UNCLAIMED, ""
                attempted = True
                with statement_timeout(5):
                    held = await self._claim(ch, key, me)
        except BaseException:
            # An insert or takeover that answered late, or whose wait was
            # cancelled, may still have landed; a claim left behind would lock the
            # key out until it went stale. Shielded, so a cancellation of this
            # request does not cancel the release too.
            if attempted:
                await self.release_shielded(ch, key, me)
            raise
        if not held:
            # Our insert may have landed with its reply lost, then read back as
            # someone else's from a lagging Keeper follower. The release matches
            # only our holder id, so it leaves a real holder's claim alone.
            await self.release_shielded(ch, key, me)
            return Claim.TAKEN, ""
        return Claim.HELD, me

    async def _claim(self, ch: Any, key: str, me: str) -> bool:
        params = {"k": key, "me": me, "stale": self.stale_s}
        try:
            await ch.execute(self.insert_sql, params=params)
            return True
        except Exception as e:
            if not key_exists(e):
                raise
        # Held: by this call when a retried insert found its own first one, else
        # by a holder that is running, or by one that died and is taken over.
        holder, stale = await self._holder(ch, key)
        if holder == me or not stale:
            return holder == me
        try:
            await ch.execute(self.take_over_sql, params=params)
        except Exception as e:
            if not lost_race(e):
                raise
        return (await self._holder(ch, key))[0] == me

    async def release(self, ch: Any, key: str, holder: str) -> None:
        """Drop this holder's claim; one taken over since is the taker's and
        stays. A release that fails or stalls leaves the claim to go stale."""
        try:
            async with asyncio.timeout(self.release_timeout_s):
                with statement_timeout(5):
                    await ch.execute(self.release_sql, params={"k": key, "me": holder})
        except Exception as e:  # noqa: BLE001 — the holder is done; a kept claim goes stale
            log.warning("%s: release of %s failed: %s", self.table, key, e)

    async def release_shielded(self, ch: Any, key: str, holder: str) -> None:
        await shielded(self.release(ch, key, holder), name=f"{self.table}.release")

    async def _holder(self, ch: Any, key: str) -> tuple[str, bool]:
        row = await ch.query_one(self.holder_sql, params={"k": key, "stale": self.stale_s})
        return (str(row["holder"]), bool(row["stale"])) if row else ("", False)

    async def available(self, ch: Any) -> bool:
        """Whether the table is here. A yes is kept; a no is asked again after
        `_ABSENT_RECHECK_S`."""
        if self.atomic is True or (self.atomic is False and time.monotonic() < self.absent_until):
            return self.atomic
        found = is_keeper_table(await ch.query_one(self.table_sql))
        if not found:
            if self.atomic is None:
                log.info("%s: no KeeperMap table (no Keeper); claims are not taken", self.table)
            self.absent_until = time.monotonic() + _ABSENT_RECHECK_S
        self.atomic = found
        return self.atomic
