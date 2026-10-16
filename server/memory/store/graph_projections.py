"""loom.graph_projections: the graph projections in flight and each namespace's last
graph write, read by every process.

Every stamp is the server's clock (`now64(3)`), so no Loom host's clock decides
when a walk is stale or when a row goes stale. The table is a KeeperMap
(`KeeperLayout`): a projection holds one of `SLOTS` keys of its namespace, claimed by
a strict INSERT and deleted at its end, or left to go stale when its begin is
uncertain; one more key holds the namespace's last graph write, raised by a
version-checked UPDATE. A walk reads only its namespace's keys, by primary key, from
the one Keeper every replica shares (read-after-write across replicas needs the
Keeper's `quorum_reads`: docs/architecture/clickhouse.md).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.datetimes import as_aware_utc
from server.core.leases import key_exists, lost_race
from server.core.repo_base import _CHClient, _default_client

TABLE = "loom.graph_projections"

# Projections of one namespace recorded at once; one more is not
# waited on by walks of other processes.
SLOTS = 32
# Tries of a version-checked raise that loses to a concurrent one.
_RAISE_TRIES = 8
_SEP = "\x1f"


@dataclass(frozen=True)
class Snapshot:
    """One read of a namespace: the server's clock at the read, the projections in
    flight (begun in the last `stale_s`), and the last graph write recorded."""

    now: datetime
    busy: frozenset[str]
    written: datetime | None


class BeginUncertain(Exception):
    """A begin that failed after it may have committed: `token` is ended anyway."""

    def __init__(self, token: str, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.token = token


def _stamp(v: Any) -> datetime | None:
    ts = as_aware_utc(v)
    # maxIf over no rows is the epoch.
    return ts if ts is not None and ts.year > 1970 else None


class KeeperLayout:
    def __init__(self) -> None:
        # Write keys this process knows exist (they are never deleted): raised by
        # UPDATE without trying the INSERT first.
        self._written: set[str] = set()
        # Per write key: the raise in flight, and the one queued after it.
        self._running: dict[str, asyncio.Task[None]] = {}
        self._queued: dict[str, asyncio.Task[None]] = {}

    _INSERT = (
        f"INSERT INTO {TABLE} (token, org, namespace, finished) "
        "SETTINGS keeper_map_strict_mode = 1, async_insert = 0 "
        "VALUES ({token:String}, {org:String}, {ns:String}, {finished:UInt8})"
    )
    _RAISE = (
        "ALTER TABLE loom.graph_projections UPDATE started_at = greatest(started_at, now64(3)) "
        "WHERE token = {token:String} SETTINGS keeper_map_strict_mode = 1"
    )
    _DROP = (
        "ALTER TABLE loom.graph_projections DELETE WHERE token = {token:String} "
        "SETTINGS keeper_map_strict_mode = 1"
    )
    _DROP_STALE = (
        "ALTER TABLE loom.graph_projections DELETE WHERE token = {token:String} "
        "AND started_at < now64(3) - toIntervalMillisecond({ms:UInt32}) "
        "SETTINGS keeper_map_strict_mode = 1"
    )
    _READ = (
        "SELECT now64(3) AS now, groupArray((token, started_at, finished)) AS keys "
        f"FROM {TABLE} WHERE token IN {{tokens:Array(String)}}"
    )

    @staticmethod
    def _slot(org: str, namespace: str, i: int) -> str:
        return f"{org}{_SEP}{namespace}{_SEP}{i}"

    @staticmethod
    def _write_key(org: str, namespace: str) -> str:
        return f"{org}{_SEP}{namespace}{_SEP}w"

    async def _read(self, ch: _CHClient, org: str, namespace: str) -> tuple[datetime, list[Any]]:
        keys = [self._slot(org, namespace, i) for i in range(SLOTS)]
        row = await ch.query_one(self._READ, {"tokens": [*keys, self._write_key(org, namespace)]})
        row = row or {}
        return as_aware_utc(row.get("now")) or datetime.min, list(row.get("keys") or [])

    async def _insert(
        self, ch: _CHClient, org: str, namespace: str, key: str, finished: int
    ) -> bool:
        """False when the key exists."""
        try:
            await ch.execute(
                self._INSERT, {"token": key, "org": org, "ns": namespace, "finished": finished}
            )
        except Exception as e:
            if key_exists(e):
                return False
            raise
        return True

    async def begin(self, ch: _CHClient, org: str, namespace: str, *, stale_s: float) -> str | None:
        """The slot key claimed, or None when every slot is held."""
        first = self._slot(org, namespace, random.randrange(SLOTS))
        if await self._claim(ch, org, namespace, first):
            return first
        for key in await self._free_slots(ch, org, namespace, stale_s=stale_s, skip=first):
            if await self._claim(ch, org, namespace, key):
                return key
        return None

    async def _claim(self, ch: _CHClient, org: str, namespace: str, key: str) -> bool:
        try:
            return await self._insert(ch, org, namespace, key, finished=0)
        except Exception as e:
            raise BeginUncertain(key, e) from e

    async def _free_slots(
        self, ch: _CHClient, org: str, namespace: str, *, stale_s: float, skip: str
    ) -> list[str]:
        """A few slot keys nobody holds, clearing those a dead process left."""
        now, rows = await self._read(ch, org, namespace)
        started = {str(token): as_aware_utc(at) or now for token, at, _ in rows}
        free = []
        for key in (self._slot(org, namespace, i) for i in range(SLOTS)):
            if key == skip:
                continue
            if key in started and (now - started[key]).total_seconds() > stale_s:
                await self._drop_stale(ch, key, stale_s)
                del started[key]
            if key not in started:
                free.append(key)
        random.shuffle(free)
        return free[:2]

    async def _drop_stale(self, ch: _CHClient, key: str, stale_s: float) -> None:
        try:
            await ch.execute(self._DROP_STALE, {"token": key, "ms": int(stale_s * 1000)})
        except Exception as e:
            if not lost_race(e):  # lost: another begin cleared it first
                raise

    async def raise_write(self, ch: _CHClient, org: str, namespace: str) -> None:
        """The namespace's last write is now. Coalesced per namespace in this process:
        one raise in flight, and the callers that arrive meanwhile share the next one,
        which starts after their writes."""
        key = self._write_key(org, namespace)
        nxt = self._queued.get(key)
        if nxt is None:
            prev = self._running.get(key)
            nxt = asyncio.ensure_future(self._raise_after(prev, ch, org, namespace))
            self._queued[key] = nxt
        await asyncio.shield(nxt)

    async def _raise_after(
        self, prev: asyncio.Task[None] | None, ch: _CHClient, org: str, namespace: str
    ) -> None:
        key = self._write_key(org, namespace)
        if prev is not None:
            await asyncio.wait([prev])
        me = self._queued.pop(key)
        self._running[key] = me
        try:
            await self._raise(ch, org, namespace, key)
        finally:
            if self._running.get(key) is me:
                del self._running[key]

    async def _raise(self, ch: _CHClient, org: str, namespace: str, key: str) -> None:
        """Version-checked: of two concurrent raises one is retried, so neither is lost."""
        if key not in self._written:
            inserted = await self._insert(ch, org, namespace, key, finished=1)
            self._written.add(key)
            if inserted:
                return
        for attempt in range(_RAISE_TRIES):
            try:
                await ch.execute(self._RAISE, {"token": key})
                return
            except Exception as e:
                if not lost_race(e) or attempt == _RAISE_TRIES - 1:
                    raise
                await asyncio.sleep(random.uniform(0, 0.005 * 2**attempt))

    async def end(
        self,
        ch: _CHClient,
        org: str,
        namespace: str,
        token: str,
        *,
        held_s: float,
        stale_s: float,
        certain: bool,
    ) -> None:
        """The slot is freed; the caller raised the write first. A projection held
        past `stale_s`, or whose begin is uncertain, leaves it to go stale: another
        projection may hold it."""
        if certain and held_s < stale_s - 1.0:
            await ch.execute(self._DROP, {"token": token})

    async def note_writes(self, ch: _CHClient, keys: list[tuple[str, str]]) -> None:
        await asyncio.gather(*(self.raise_write(ch, org, ns) for org, ns in keys))

    async def snapshot(
        self, ch: _CHClient, org: str, namespace: str, *, stale_s: float
    ) -> Snapshot:
        now, rows = await self._read(ch, org, namespace)
        busy: set[str] = set()
        written: datetime | None = None
        for token, started, finished in rows:
            at = as_aware_utc(started)
            if at is None:
                continue
            if int(finished):
                written = at
            elif (now - at).total_seconds() < stale_s:
                # A slot taken again is another projection: its start names it.
                busy.add(f"{token}@{at.isoformat()}")
        return Snapshot(now=now, busy=frozenset(busy), written=written)


_layout = KeeperLayout()


async def begin(
    org: str, namespace: str, *, stale_s: float, ch: _CHClient | None = None
) -> str | None:
    """Record a projection of (org, namespace) in flight; its token, or None when
    none was recorded. Raises `BeginUncertain` when it may have been."""
    ch = ch or _default_client()
    return await _layout.begin(ch, org, namespace, stale_s=stale_s)


async def end(
    org: str,
    namespace: str,
    token: str,
    *,
    held_s: float,
    stale_s: float,
    certain: bool = True,
    ch: _CHClient | None = None,
) -> None:
    """The projection `token` ended `held_s` after its begin (`certain`: the begin
    is known to have committed). The caller records its write first (`note_writes`)."""
    ch = ch or _default_client()
    layout = _layout
    await layout.end(ch, org, namespace, token, held_s=held_s, stale_s=stale_s, certain=certain)


async def note_writes(keys: list[tuple[str, str]], *, ch: _CHClient | None = None) -> None:
    """Each (org, namespace) in `keys` was written to now (the server's clock)."""
    ch = ch or _default_client()
    await _layout.note_writes(ch, keys)


async def snapshot(
    org: str, namespace: str, *, stale_s: float, ch: _CHClient | None = None
) -> Snapshot:
    ch = ch or _default_client()
    return await _layout.snapshot(ch, org, namespace, stale_s=stale_s)


def _reset_for_tests() -> None:
    global _layout
    _layout = KeeperLayout()
