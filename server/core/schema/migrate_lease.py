"""The boot-time migration lease.

Two implementations share one shape (`acquire` / `held` / `outcome` /
`waited_s` / `renew_if_due` / `release`). `KeeperLock` is the lock: one
KeeperMap key under `keeper_map_strict_mode = 1`, where Keeper rejects an
INSERT of an existing key and checks the znode version on every UPDATE, so of
two contenders exactly one write lands. `MigrationLease`, below, is the
fallback for a server without a Keeper.

`server/core/schema/migrate.py` runs in every loom process at boot. The processes of one
container take an fcntl file lock in turn; two CONTAINERS of one colour (web +
workers) share no filesystem, so without a shared lock both can find the same
migrations pending, apply them and record them twice. This module serialises the appliers across
containers with one row per contender in `loom._migration_lease`:

    insert my row → settle → read every row back (sequential consistency)
    → hold the lease when mine is the earliest-acquired live row
    → otherwise poll until the holder releases or its lease expires.

A row is live while `released = false` and `renewed_at + ttl_s` has not
passed — both on the server's clock, so contenders on different hosts order
the rows the same way. The holder renews between migration files and
releases when done; a holder that crashes stops renewing and is replaced
after `ttl_s`. `acquired_at` never changes, so a contender's place in the
queue is fixed the moment its first row lands; a tie on the millisecond is
broken by the holder string.

Fail-open like the file lock only before this contender's row lands: a
database the migrator has not reached yet has no lease table, and a first
insert the server refuses is reported and the run proceeds unleased — file
lock only, with the `_record` belt (the ledger re-read for the filename
before it is written) still in place. Once the row has landed, a failed
read-back is retried until `wait_s` (then `timeout`), never read as "no
holder".
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.ch_errors import error_code
from server.core.datetimes import as_aware_utc

LEASE_TABLE = "loom._migration_lease"
LEASE_NAME = "boot_migrate"

# Every contender's latest row (FINAL: a renewal or release supersedes) with
# its liveness decided on the server's clock. Sequential consistency: every
# insert the service acknowledged before this read began is visible.
ROWS_SQL = (
    "SELECT holder, acquired_at, renewed_at, ttl_s, released, "
    "(renewed_at + toIntervalSecond(ttl_s)) > now64(3) AS live "
    f"FROM {LEASE_TABLE} FINAL WHERE name = {{name:String}}"
)
ROWS_SETTINGS = {"select_sequential_consistency": 1}


@dataclass(frozen=True)
class LeaseConfig:
    """`ttl_s`: a holder that stops renewing is replaced after this long.
    `wait_s`: how long a contender waits for the holder before giving up.
    `settle_s`: the pause between a contender's insert and its first read-back.
    `poll_s` / `poll_max_s`: the wait between read-backs, growing 1.5× a poll."""

    ttl_s: int = 300
    wait_s: float = 900.0
    settle_s: float = 1.0
    poll_s: float = 2.0
    poll_max_s: float = 10.0


@dataclass(frozen=True)
class LeaseRow:
    holder: str
    acquired_at: datetime
    renewed_at: datetime
    ttl_s: int
    released: bool
    live: bool


def winner(rows: list[LeaseRow]) -> LeaseRow | None:
    """The earliest-acquired live row: the contender that holds the lease.
    Every contender computes this over the same rows, so they agree."""
    live = [r for r in rows if not r.released and r.live]
    if not live:
        return None
    return min(live, key=lambda r: (r.acquired_at, r.holder))


def default_holder() -> str:
    return f"{socket.gethostname()[:64]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def describe_error(e: BaseException) -> str:
    if error_code(e) == 60:
        return f"table {LEASE_TABLE} absent (not applied here yet)"
    return f"{type(e).__name__}: {str(e)[:160]}"


class MigrationLease:
    """One contender's view of the lease.

    `acquire()` returns `"held"`, `"timeout"` (the holder never released
    within `wait_s`, or read-backs kept failing that long) or `"unleased"`
    (no table, a first insert the server refused, or a server that never
    shows my row: the caller runs without the lease). `release()` is safe to call
    after any outcome. `sleep` / `clock` / `out` are injectable for tests."""

    kind = "table"

    def __init__(
        self,
        ch: Any,
        *,
        cfg: LeaseConfig | None = None,
        holder: str | None = None,
        build_commit: str = "",
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = print,
    ) -> None:
        self.ch = ch
        self.cfg = cfg or LeaseConfig()
        self.holder = holder or default_holder()
        self.build_commit = build_commit[:64]
        self.sleep = sleep
        self.clock = clock
        self.out = out
        self.held = False
        self.outcome = ""
        self.waited_s = 0.0
        self._acquired_at: datetime | None = None
        self._inserted = False
        self._version = 0
        self._last_renew = 0.0

    # -- rows -----------------------------------------------------------------

    def _next_version(self) -> int:
        # The holder's own clock, monotonic within this process; `version` only
        # competes with this holder's earlier rows (ORDER BY (name, holder)).
        self._version = max(time.time_ns(), self._version + 1)
        return self._version

    def _insert(self, *, released: bool) -> None:
        cols = ["name", "holder", "ttl_s", "released", "build_commit", "version"]
        row: list[Any] = [
            LEASE_NAME,
            self.holder,
            int(self.cfg.ttl_s),
            released,
            self.build_commit,
            self._next_version(),
        ]
        if self._acquired_at is not None:
            # A renewal or release keeps the queue position; the first insert
            # lets the server stamp acquired_at (and every renewed_at). The value
            # read back is naive UTC, and the driver reads a naive one as local.
            cols.append("acquired_at")
            row.append(as_aware_utc(self._acquired_at))
        self.ch.insert(LEASE_TABLE, [row], column_names=cols)
        self._inserted = True
        self._last_renew = self.clock()

    def _rows(self) -> list[LeaseRow]:
        rows = self.ch.query(
            ROWS_SQL, parameters={"name": LEASE_NAME}, settings=ROWS_SETTINGS
        ).result_rows
        return [
            LeaseRow(
                holder=str(r[0]),
                acquired_at=r[1],
                renewed_at=r[2],
                ttl_s=int(r[3]),
                released=bool(r[4]),
                live=bool(r[5]),
            )
            for r in rows
        ]

    # -- protocol -------------------------------------------------------------

    def acquire(self) -> str:
        started = self.clock()
        try:
            self._insert(released=False)
            self.sleep(self.cfg.settle_s)
            self.outcome = self._wait(started)
        except Exception as e:  # noqa: BLE001 — fail open, like the file lock
            self.outcome = "unleased"
            self.out(
                f"  · migration lease: {describe_error(e)} — running without it "
                "(file lock only; the ledger is re-read before every record)"
            )
        return self.outcome

    def _wait(self, started: float) -> str:
        poll = self.cfg.poll_s
        announced: str | None = None
        unseen = 0
        while True:
            try:
                rows: list[LeaseRow] | None = self._rows()
            except Exception as e:  # noqa: BLE001 — my row landed: keep waiting, bounded by wait_s
                # The table exists (my insert landed), so a failed read-back is
                # no evidence the holder is gone: running unleased now could
                # apply beside it. Retry until wait_s, then time out.
                self.out(f"  · migration lease: read-back failed ({describe_error(e)}) — retrying")
                rows = None
            if rows is not None:
                mine = next((r for r in rows if r.holder == self.holder), None)
                if mine is not None and self._acquired_at is None:
                    self._acquired_at = mine.acquired_at
                if mine is None and self._acquired_at is None:
                    # My own acknowledged insert is in a sequentially consistent
                    # read; a server (or a fake) that never shows it would hold
                    # the boot for wait_s. Three misses: run without the lease.
                    unseen += 1
                    if unseen >= 3:
                        raise RuntimeError("own lease row not visible after 3 read-backs")
                top = winner(rows)
                if mine is not None and top is not None and top.holder == self.holder:
                    self.waited_s = self.clock() - started
                    self.held = True
                    return "held"
                if top is not None and top.holder != announced:
                    announced = top.holder
                    self.out(
                        f"  · migration lease: held by {top.holder} since "
                        f"{top.acquired_at:%H:%M:%S}Z (ttl {top.ttl_s} s) — waiting"
                    )
            self.waited_s = self.clock() - started
            if self.waited_s >= self.cfg.wait_s:
                return "timeout"
            self.renew_if_due()
            self.sleep(poll)
            poll = min(poll * 1.5, self.cfg.poll_max_s)

    def renew_if_due(self) -> None:
        """Re-stamp `renewed_at` once a third of the TTL has passed, so a
        holder applying a long file (or a contender waiting) stays live.
        Only once the first row has been read back: a renewal before that
        would change the queue position.

        A holder reads the rows first. When one statement outlasted the TTL
        its row expired, and a contender may have taken the lease since;
        renewing then would make the old row live again with the earlier
        `acquired_at` and two processes would apply at once. A live row of
        another holder at the top means the lease is lost: `held` goes
        False (the runner stops) and nothing is re-inserted. A failed read
        renews only while the row cannot have expired yet (less than `ttl_s`
        since the last renewal); past that the lease counts as lost."""
        if self._acquired_at is None:
            return
        if self.clock() - self._last_renew < self.cfg.ttl_s / 3:
            return
        if self.held:
            try:
                top = winner(self._rows())
            except Exception as e:  # noqa: BLE001 — fails closed once the row may have expired
                self.out(f"  · migration lease: holder check failed ({describe_error(e)})")
                if self.clock() - self._last_renew >= self.cfg.ttl_s:
                    self.held = False
                    self.out("  · migration lease: lost (row may have expired, holder unknown)")
                    return
                top = None
            if top is not None and top.holder != self.holder:
                self.held = False
                self.out(f"  · migration lease: expired while applying; now held by {top.holder}")
                return
        try:
            self._insert(released=False)
        except Exception as e:  # noqa: BLE001 — a missed renewal is a WARNING
            self.out(f"  · migration lease: renewal failed ({describe_error(e)})")

    def release(self) -> None:
        if not self._inserted:
            return
        try:
            self._insert(released=True)
        except Exception as e:  # noqa: BLE001 — the row expires by TTL
            self.out(f"  · migration lease: release failed ({describe_error(e)})")
        self.held = False
        self._inserted = False


LOCK_TABLE = "loom._migration_lock"
# Tables the runner creates itself, before any migration file runs.
RUNNER_TABLES: frozenset[str] = frozenset({LOCK_TABLE.removeprefix("loom.")})
LOCK_DDL = (
    f"CREATE TABLE IF NOT EXISTS {LOCK_TABLE} "
    "(name String, holder String, acquired_at DateTime64(3), renewed_at DateTime64(3), "
    "ttl_s UInt32, build_commit String) "
    "ENGINE = KeeperMap('/loom/migration_lock') PRIMARY KEY name"
)
# No Keeper or no keeper_map_path_prefix: the tolerated codes of the task lease table.
NO_KEEPER_CODES = frozenset({999, 139, 36})
_STRICT = "SETTINGS keeper_map_strict_mode = 1"
LOCK_INSERT = (
    f"INSERT INTO {LOCK_TABLE} (name, holder, acquired_at, renewed_at, ttl_s, build_commit) "
    f"{_STRICT}, async_insert = 0 "
    "VALUES ({name:String}, {me:String}, now64(3), now64(3), {ttl:UInt32}, {commit:String})"
)
LOCK_ROW = (
    "SELECT holder, acquired_at, ttl_s, (renewed_at + toIntervalSecond(ttl_s)) < now64(3) "
    f"FROM {LOCK_TABLE} WHERE name = {{name:String}}"
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


def key_exists(e: BaseException) -> bool:
    return "Node exists" in str(e)


# Run ids are claimed in Keeper only where 025_agent_run_claims.sql created its
# KeeperMap table (a server with a Keeper). run.begin, erasure and the doctor
# all decide it with this one read and `is_keeper_table`.
RUN_CLAIMS_TABLE_SQL = (
    "SELECT engine FROM system.tables WHERE database = 'loom' AND name = 'agent_run_claims'"
)


def is_keeper_table(row: dict[str, Any] | None) -> bool:
    return row is not None and row.get("engine") == "KeeperMap"


def lost_race(e: BaseException) -> bool:
    return "Bad version" in str(e) or "KEEPER_EXCEPTION" in str(e)


class KeeperLock:
    """The migration lock as one KeeperMap key.

    Acquire inserts the key; a duplicate means someone holds it. A holder whose
    `renewed_at + ttl_s` has passed (server clock) is taken over by a
    conditional UPDATE naming it; Keeper's version check lets one taker win.
    Renew and release are conditional on `holder = me`, so a process that lost
    the lock changes nothing. `outcome` is `held`, `timeout`, or `unleased`
    (Keeper refused the first insert: the caller falls back)."""

    kind = "keeper"

    def __init__(
        self,
        ch: Any,
        *,
        cfg: LeaseConfig | None = None,
        holder: str | None = None,
        build_commit: str = "",
        lock_name: str = LEASE_NAME,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = print,
    ) -> None:
        self.ch = ch
        self.cfg = cfg or LeaseConfig()
        self.holder = holder or default_holder()
        self.build_commit = build_commit[:64]
        self.lock_name = lock_name
        self.sleep = sleep
        self.clock = clock
        self.out = out
        self.held = False
        self.outcome = ""
        self.waited_s = 0.0
        self.error = ""
        self._last_renew = 0.0
        self._mutex = threading.Lock()
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None

    def _params(self, **extra: Any) -> dict[str, Any]:
        return {
            "name": self.lock_name,
            "me": self.holder,
            "ttl": int(self.cfg.ttl_s),
            "commit": self.build_commit,
            **extra,
        }

    def _holder_row(self) -> tuple[str, datetime, int, bool] | None:
        rows = self.ch.query(LOCK_ROW, parameters={"name": self.lock_name}).result_rows
        if not rows:
            return None
        r = rows[0]
        return str(r[0]), r[1], int(r[2]), bool(r[3])

    def _try_insert(self) -> bool:
        try:
            self.ch.command(LOCK_INSERT, parameters=self._params())
            return True
        except Exception as e:
            if key_exists(e):
                return False
            raise

    def _try_takeover(self, stale: str) -> bool:
        try:
            self.ch.command(LOCK_TAKEOVER, parameters=self._params(old=stale))
        except Exception as e:
            if not lost_race(e):
                raise
        row = self._holder_row()
        return row is not None and row[0] == self.holder

    def _attempt(self) -> tuple[bool, tuple[str, datetime, int, bool] | None]:
        """One round: insert, else take over a stale holder. (won, holder row)."""
        if self._try_insert():
            return True, None
        row = self._holder_row()
        if row is None:  # released between the insert and the read
            return False, None
        if row[3] and self._try_takeover(row[0]):
            self.out(f"  · migration lock: took over from {row[0]} (stopped renewing)")
            return True, None
        return False, row

    def acquire(self) -> str:
        started = self.clock()
        try:
            won, row = self._attempt()
        except Exception as e:  # noqa: BLE001 — the caller falls back to the lease table
            self.error = describe_error(e)
            self.outcome = "unleased"
            return self.outcome
        poll = self.cfg.poll_s
        announced: str | None = None
        while not won:
            self.waited_s = self.clock() - started
            if self.waited_s >= self.cfg.wait_s:
                self.outcome = "timeout"
                return self.outcome
            if row is not None and row[0] != announced:
                announced = row[0]
                self.out(
                    f"  · migration lock: held by {row[0]} since "
                    f"{row[1]:%H:%M:%S}Z (ttl {row[2]} s) — waiting"
                )
            self.sleep(poll)
            poll = min(poll * 1.5, self.cfg.poll_max_s)
            try:
                won, row = self._attempt()
            except Exception as e:  # noqa: BLE001 — a failed round; the wait is bounded
                self.out(f"  · migration lock: {describe_error(e)} — retrying")
                won, row = False, None
        self.waited_s = self.clock() - started
        self.held = True
        self._last_renew = self.clock()
        self.outcome = "held"
        return self.outcome

    def renew_if_due(self) -> None:
        """Re-stamp `renewed_at` once a third of the TTL has passed. A renewal
        that finds another holder clears `held`: the lock was taken over."""
        self._renew(self.ch)

    def _renew(self, ch: Any, *, force: bool = False) -> None:
        with self._mutex:
            if not self.held:
                return
            if not force and self.clock() - self._last_renew < self.cfg.ttl_s / 3:
                return
            try:
                ch.command(LOCK_RENEW, parameters=self._params())
                rows = ch.query(LOCK_ROW, parameters={"name": self.lock_name}).result_rows
            except Exception as e:  # noqa: BLE001 — a missed renewal is a WARNING
                self.out(f"  · migration lock: renewal failed ({describe_error(e)})")
                return
            self._last_renew = self.clock()
            now = str(rows[0][0]) if rows else ""
            if now != self.holder:
                self.held = False
                self.out(f"  · migration lock: lost to {now or 'nobody'} (renewal came too late)")

    def start_heartbeat(self, client: Callable[[], Any]) -> None:
        """Renew from a thread on its own connection, so a statement that
        blocks for longer than the TTL (a mutation waited on every replica)
        does not let the lock expire under it."""
        if not self.held or self._beat is not None:
            return
        try:
            ch = client()
        except Exception as e:  # noqa: BLE001 — renewals between files remain
            self.out(f"  · migration lock: no heartbeat connection ({describe_error(e)})")
            return
        interval = max(self.cfg.ttl_s / 3, 1.0)

        def beat() -> None:
            while not self._stop.wait(interval):
                self._renew(ch, force=True)

        self._beat = threading.Thread(target=beat, name="migration-lock-heartbeat", daemon=True)
        self._beat.start()

    def release(self) -> None:
        self._stop.set()
        if self._beat is not None:
            self._beat.join(timeout=5)
            self._beat = None
        with self._mutex:
            if not self.held:
                return
            try:
                self.ch.command(LOCK_RELEASE, parameters=self._params())
            except Exception as e:  # noqa: BLE001 — the key expires by TTL
                self.out(f"  · migration lock: release failed ({describe_error(e)})")
            self.held = False


def bootstrap_keeper_lock(ch: Any) -> str | None:
    """Create the lock table. None when it is usable, else why not."""
    try:
        ch.command(LOCK_DDL)
        return None
    except Exception as e:  # noqa: BLE001 — every refusal means the fallback lease
        code = error_code(e)
        if code in NO_KEEPER_CODES:
            return f"no Keeper on this server (code {code})"
        return describe_error(e)


__all__ = [
    "LEASE_NAME",
    "LEASE_TABLE",
    "LOCK_DDL",
    "LOCK_TABLE",
    "NO_KEEPER_CODES",
    "KeeperLock",
    "LOCK_INSERT",
    "LOCK_RELEASE",
    "LOCK_TAKEOVER",
    "bootstrap_keeper_lock",
    "ROWS_SETTINGS",
    "ROWS_SQL",
    "LeaseConfig",
    "LeaseRow",
    "MigrationLease",
    "default_holder",
    "key_exists",
    "lost_race",
    "describe_error",
    "winner",
]
