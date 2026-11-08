"""The blocking variant of `server.core.leases`, for the callers that cannot await:
the boot migrator (`server/core/schema/migrate.py`, before the event loop and in
`loom migrate`) and the workers supervisor's lease, stepped from a thread on its own
short-timeout client so a hung call never stalls the serving loop.

`KeeperLock` is one key of the KeeperMap table `loom._migration_lock` under
`keeper_map_strict_mode = 1` (`acquire` / `held` / `outcome` / `waited_s` /
`renew_if_due` / `release` / `peek_free`). `cluster_lock` gives one for a named key.
The boot migrator takes `boot_migrate` before applying, so two containers never
apply the same pending files. A server without a Keeper raises `KeeperRequired`:
ClickHouse has no atomic arbitration without one, so Loom does not run there.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.ch_errors import error_code
from server.core.errors import error_kind
from server.core.leases import (
    ENGINE_SQL,
    LOCK_HOLDER,
    LOCK_INSERT,
    LOCK_RELEASE,
    LOCK_RENEW,
    LOCK_TABLE,
    LOCK_TAKEOVER,
    default_holder,
    key_exists,
    lost_race,
)

LEASE_NAME = "boot_migrate"


@dataclass(frozen=True)
class LeaseConfig:
    """`ttl_s`: a holder that stops renewing is replaced after this long.
    `wait_s`: how long a contender waits for the holder before giving up.
    `poll_s` / `poll_max_s`: the wait between read-backs, growing 1.5× a poll.
    `renew_after_s`: a renewal is due this long after the last one (default a
    third of the TTL).
    `lose_after_s`: a holder that has not renewed for this long counts the lease
    lost (default half the TTL), well before another can take it over. Both are
    measured from when the last good renewal was sent, never from its answer."""

    ttl_s: int = 300
    wait_s: float = 900.0
    poll_s: float = 2.0
    poll_max_s: float = 10.0
    renew_after_s: float | None = None
    lose_after_s: float | None = None

    @property
    def renew_after(self) -> float:
        return self.renew_after_s if self.renew_after_s is not None else self.ttl_s / 3

    @property
    def lose_after(self) -> float:
        return self.lose_after_s if self.lose_after_s is not None else self.ttl_s / 2


def describe_error(e: BaseException) -> str:
    """The lock's error as `error` keeps it, which `/v1/health` shows without a token
    (the workers lease's reason): the absent table, or `error_kind(e)`. The driver's
    text, which can carry the server's address, is in `operator_error_text`."""
    return _absent_table(e) or error_kind(e)


def operator_error_text(e: BaseException) -> str:
    """The lock's error with the driver's text, for the operator's output lines."""
    return _absent_table(e) or f"{type(e).__name__}: {str(e)[:160]}"


def _absent_table(e: BaseException) -> str:
    """The sentence for a server without the lock table (UNKNOWN_TABLE), else ''."""
    return f"table {LOCK_TABLE} absent" if error_code(e) == 60 else ""


# Tables the runner creates itself, before any migration file runs.
RUNNER_TABLES: frozenset[str] = frozenset({LOCK_TABLE.removeprefix("loom.")})
LOCK_DDL = (
    f"CREATE TABLE IF NOT EXISTS {LOCK_TABLE} "
    "(name String, holder String, acquired_at DateTime64(3), renewed_at DateTime64(3), "
    "ttl_s UInt32, build_commit String) "
    "ENGINE = KeeperMap('/loom/migration_lock') PRIMARY KEY name"
)
# A server with no Keeper configured (no keeper_map_path_prefix).
NO_KEEPER_CODES = frozenset({139, 36})


class KeeperRequired(RuntimeError):
    """The server has no ClickHouse Keeper (or no `keeper_map_path_prefix`)."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"Loom needs a ClickHouse Keeper ({detail}): its leases and claims are "
            "KeeperMap keys. Run ClickHouse with deploy/clickhouse-config/keeper.xml "
            "(the bundled stacks do), or point LOOM_CH_* at a server that has one "
            "(ClickHouse Cloud does)."
        )


# A Keeper that is there but failing: retried, never a fallback.
KEEPER_ERROR_CODE = 999


class KeeperLock:
    """The migration lock as one KeeperMap key.

    Acquire inserts the key; a duplicate means someone holds it. A holder whose
    `renewed_at + ttl_s` has passed (server clock) is taken over by a
    conditional UPDATE naming it; Keeper's version check lets one taker win.
    Renew and release are conditional on `holder = me`, so a process that lost
    the lock changes nothing. `outcome` is `held`, `timeout`, or `unleased`
    (the server refused the first statement: the caller stops)."""

    kind = "keeper"

    def __init__(
        self,
        ch: Any,
        *,
        cfg: LeaseConfig | None = None,
        holder: str | None = None,
        build_commit: str = "",
        lock_name: str = LEASE_NAME,
        label: str = "migration",
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = print,
    ) -> None:
        self.ch = ch
        self.cfg = cfg or LeaseConfig()
        self.holder = holder or default_holder()
        self.build_commit = build_commit[:64]
        self.lock_name = lock_name
        self.label = label
        self.sleep = sleep
        self.clock = clock
        self.out = out
        self.held = False
        self.outcome = ""
        self.waited_s = 0.0
        # The last failed round's error ('' after a clean one), and its code.
        self.error = ""
        # The same error with the driver's text, for the operator's output only.
        self.error_detail = ""
        self.error_code: int | None = None
        self._ever_held = False
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
        rows = self.ch.query(LOCK_HOLDER, parameters={"name": self.lock_name}).result_rows
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

    def _try_retake(self) -> bool:
        """The key still names me (I counted it lost after failed renewals and
        nobody took it over): renew it back. Conditional on `holder = me`."""
        self.ch.command(LOCK_RENEW, parameters=self._params())
        row = self._holder_row()
        return row is not None and row[0] == self.holder

    def _attempt(self) -> tuple[bool, tuple[str, datetime, int, bool] | None]:
        """One round: read the key; insert when there is none, else take my
        own key back, else take over a stale holder. (won, holder row). The
        read comes first: an INSERT that finds the key is a KEEPER_EXCEPTION
        in the server's log, every round of every waiting contender."""
        sent = self.clock()
        row = self._holder_row()
        if row is None:
            if self._try_insert():
                self._last_renew = sent
                return True, None
            row = self._holder_row()
            if row is None:  # released between the insert and the read
                return False, None
        if row[0] == self.holder and self._try_retake():
            self._last_renew = sent
            self.out(f"  · {self.label} lock: renewed again after missed renewals")
            return True, None
        if row[3] and self._try_takeover(row[0]):
            self._last_renew = sent
            self.out(f"  · {self.label} lock: took over from {row[0]} (stopped renewing)")
            return True, None
        return False, row

    @property
    def last_renew(self) -> float:
        """When the last renewal that landed was sent (`clock`)."""
        return self._last_renew

    def acquire(self) -> str:
        started = self.clock()
        try:
            won, row = self._attempt()
            self.error, self.error_detail, self.error_code = "", "", None
        except Exception as e:  # noqa: BLE001 — a Keeper or network error retries below; a refusal stops
            self.error, self.error_code = describe_error(e), error_code(e)
            self.error_detail = operator_error_text(e)
            if not _transient(e):
                self.outcome = "unleased"
                return self.outcome
            self.out(f"  · {self.label} lock: {self.error_detail} — retrying")
            won, row = False, None
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
                    f"  · {self.label} lock: held by {row[0]} since "
                    f"{row[1]:%H:%M:%S}Z (ttl {row[2]} s) — waiting"
                )
            self.sleep(poll)
            poll = min(poll * 1.5, self.cfg.poll_max_s)
            try:
                won, row = self._attempt()
                self.error, self.error_detail, self.error_code = "", "", None
            except Exception as e:  # noqa: BLE001 — a failed round; the wait is bounded
                self.error, self.error_code = describe_error(e), error_code(e)
                self.error_detail = operator_error_text(e)
                self.out(f"  · {self.label} lock: {self.error_detail} — retrying")
                won, row = False, None
        self.waited_s = self.clock() - started
        self.held = self._ever_held = True
        self.outcome = "held"
        return self.outcome

    def peek_free(self) -> bool:
        """No key, or a stale one: free to take (one read, no write)."""
        row = self._holder_row()
        return row is None or row[3] or row[0] == self.holder

    def renew_if_due(self) -> None:
        """Re-stamp `renewed_at` once `renew_after` has passed. A renewal
        that finds another holder clears `held`: the lock was taken over."""
        self._renew(self.ch)

    def _renew(self, ch: Any, *, force: bool = False) -> None:
        with self._mutex:
            if not self.held:
                return
            if not force and self.clock() - self._last_renew < self.cfg.renew_after:
                return
            sent = self.clock()
            try:
                ch.command(LOCK_RENEW, parameters=self._params())
                rows = ch.query(LOCK_HOLDER, parameters={"name": self.lock_name}).result_rows
            except Exception as e:  # noqa: BLE001 — a missed renewal is a WARNING until lose_after
                self.out(f"  · {self.label} lock: renewal failed ({operator_error_text(e)})")
                if self.clock() - self._last_renew >= self.cfg.lose_after:
                    # No renewal for lose_after: stop while the key is still
                    # ours, before another process can take it over.
                    self.held = False
                    self.out(f"  · {self.label} lock: lost (no renewal in time)")
                return
            now = str(rows[0][0]) if rows else ""
            if now == self.holder:
                self._last_renew = sent
            else:
                self.held = False
                self.out(
                    f"  · {self.label} lock: lost to {now or 'nobody'} (renewal came too late)"
                )

    def start_heartbeat(self, client: Callable[[], Any]) -> None:
        """Renew from a thread on its own connection, so a statement that
        blocks for longer than the TTL (a mutation waited on every replica)
        does not let the lock expire under it."""
        if not self.held or self._beat is not None:
            return
        try:
            ch = client()
        except Exception as e:  # noqa: BLE001 — renewals between files remain
            self.out(f"  · {self.label} lock: no heartbeat connection ({operator_error_text(e)})")
            return
        interval = max(self.cfg.ttl_s / 3, 1.0)

        def beat() -> None:
            try:
                while not self._stop.wait(interval):
                    self._renew(ch, force=True)
            finally:
                with contextlib.suppress(Exception):
                    ch.close()

        self._beat = threading.Thread(target=beat, name="migration-lock-heartbeat", daemon=True)
        self._beat.start()

    def release(self) -> None:
        self._stop.set()
        if self._beat is not None:
            self._beat.join(timeout=5)  # not a setting: a heartbeat thread's exit on release
            self._beat = None
        with self._mutex:
            # A holder that counted the key lost still deletes it while it
            # names this holder (conditional), so a successor need not wait T.
            if not self._ever_held:
                return
            try:
                self.ch.command(LOCK_RELEASE, parameters=self._params())
            except Exception as e:  # noqa: BLE001 — the key expires by TTL
                self.out(f"  · {self.label} lock: release failed ({operator_error_text(e)})")
            self.held = self._ever_held = False


def _transient(e: BaseException) -> bool:
    """A Keeper error or no server answer at all: worth a retry. A server
    refusal (privileges, a bad argument) is not."""
    code = error_code(e)
    return code is None or code == KEEPER_ERROR_CODE


class KeeperUnavailable(RuntimeError):
    """The Keeper (or the server) kept failing for the whole wait."""


def bootstrap_keeper_lock(
    ch: Any,
    *,
    wait_s: float = 0.0,
    poll_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Create the lock table. KeeperRequired when the server has no Keeper; a
    Keeper error is retried for `wait_s`, then raises `KeeperUnavailable`; any
    other refusal raises as it is."""
    started = clock()
    while True:
        try:
            ch.command(LOCK_DDL)
            return
        except Exception as e:  # a missing Keeper stops, a Keeper error is retried
            code = error_code(e)
            if code in NO_KEEPER_CODES:
                raise KeeperRequired(f"code {code}") from e
            if not _transient(e):
                raise
            if clock() - started >= wait_s:
                raise KeeperUnavailable(operator_error_text(e)) from e
        sleep(poll_s)


def cluster_lock(
    ch: Any,
    *,
    name: str,
    label: str,
    cfg: LeaseConfig,
    out: Callable[[str], None] = print,
) -> KeeperLock:
    """A lock named `name` that every process of the deployment sees: a key of
    the Keeper lock table. KeeperRequired without a Keeper; any other error
    raises (the caller retries)."""
    try:
        rows = ch.query(ENGINE_SQL, parameters={"table": "_migration_lock"}).result_rows
        if not rows or rows[0][0] != "KeeperMap":
            ch.command(LOCK_DDL)
    except Exception as e:
        if error_code(e) in NO_KEEPER_CODES:
            raise KeeperRequired(f"code {error_code(e)}") from e
        raise
    return KeeperLock(ch, cfg=cfg, lock_name=name, label=label, out=out)


__all__ = [
    "KEEPER_ERROR_CODE",
    "LEASE_NAME",
    "LOCK_DDL",
    "NO_KEEPER_CODES",
    "RUNNER_TABLES",
    "KeeperLock",
    "KeeperRequired",
    "KeeperUnavailable",
    "LeaseConfig",
    "bootstrap_keeper_lock",
    "cluster_lock",
    "describe_error",
    "operator_error_text",
]
