"""The migration lock on a KeeperMap key, and the runner's choice between it
and the lease table.

CH-free. `FakeKeeper` holds the one key the way KeeperMap under strict mode
does: an INSERT of an existing key fails with "Node exists", and an UPDATE
evaluates its WHERE on the current row. `before_update` hooks inject a racing
writer so a stale takeover can lose with "Bad version"."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from server.core.schema import migrate as mig
from server.core.schema.journal import sorted_migrations
from server.core.schema.migrate_lease import (
    LEASE_NAME,
    LOCK_DDL,
    LOCK_TABLE,
    KeeperLock,
    LeaseConfig,
    MigrationLease,
    bootstrap_keeper_lock,
)

T0 = datetime(2026, 1, 1, 12, 0, 0)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s

    @property
    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.t)


class FakeKeeper:
    def __init__(self, clock: Clock, *, create_error: str | None = None) -> None:
        self.clock = clock
        self.create_error = create_error
        self.insert_error: str | None = None
        self.row: dict | None = None
        self.commands: list[tuple[str, dict]] = []
        self.before_update: list[Callable[[], None]] = []

    def seed(self, holder: str, *, renewed_s: float, ttl_s: int = 300) -> None:
        self.row = {
            "holder": holder,
            "acquired_at": T0 + timedelta(seconds=renewed_s),
            "renewed_at": T0 + timedelta(seconds=renewed_s),
            "ttl_s": ttl_s,
        }

    def _stale(self) -> bool:
        r = self.row
        return r is not None and r["renewed_at"] + timedelta(seconds=r["ttl_s"]) < self.clock.now

    def command(self, sql: str, parameters=None, settings=None):
        p = parameters or {}
        self.commands.append((sql, p))
        if sql == LOCK_DDL:
            if self.create_error:
                raise RuntimeError(self.create_error)
            return None
        assert LOCK_TABLE in sql and "keeper_map_strict_mode = 1" in sql
        assert p.get("name") == LEASE_NAME
        now = self.clock.now
        if sql.startswith("INSERT"):
            if self.insert_error:
                raise RuntimeError(self.insert_error)
            if self.row is not None:
                raise RuntimeError(
                    "Code: 999. DB::Exception: Transaction failed (Node exists): Op #0. "
                    "(KEEPER_EXCEPTION)"
                )
            self.row = {"holder": p["me"], "acquired_at": now, "renewed_at": now, "ttl_s": p["ttl"]}
        elif " UPDATE holder = " in sql:  # takeover
            if self.before_update:
                self.before_update.pop(0)()
                raise RuntimeError(
                    "Code: 999. Transaction failed (Bad version). (KEEPER_EXCEPTION)"
                )
            if self.row and self.row["holder"] == p["old"] and self._stale():
                self.row = {
                    "holder": p["me"],
                    "acquired_at": now,
                    "renewed_at": now,
                    "ttl_s": p["ttl"],
                }
        elif " UPDATE renewed_at = " in sql:
            if self.row and self.row["holder"] == p["me"]:
                self.row["renewed_at"] = now
        elif " DELETE " in sql:
            if self.row and self.row["holder"] == p["me"]:
                self.row = None
        else:
            raise AssertionError(sql)
        return None

    def query(self, sql: str, parameters=None, settings=None):
        assert LOCK_TABLE in sql and parameters == {"name": LEASE_NAME}
        if self.row is None:
            return SimpleNamespace(result_rows=[])
        r = self.row
        return SimpleNamespace(
            result_rows=[(r["holder"], r["acquired_at"], r["ttl_s"], self._stale())]
        )


def _lock(ch: FakeKeeper, clock: Clock, holder: str, out: list[str], **cfg) -> KeeperLock:
    conf = LeaseConfig(**{"poll_s": 1.0, "poll_max_s": 4.0, "wait_s": 60.0, **cfg})
    return KeeperLock(
        ch,
        cfg=conf,
        holder=holder,
        build_commit="abc123",
        sleep=clock.sleep,
        clock=clock.monotonic,
        out=out.append,
    )


def test_acquire_inserts_the_key_and_release_deletes_it() -> None:
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    lock = _lock(ch, clock, "web:1:aa", out)
    assert lock.acquire() == "held" and lock.held and lock.waited_s == 0.0
    sql, p = ch.commands[0]
    assert sql.startswith("INSERT") and "async_insert = 0" in sql
    assert p == {"name": LEASE_NAME, "me": "web:1:aa", "ttl": 300, "commit": "abc123"}
    assert ch.row is not None and ch.row["holder"] == "web:1:aa"
    lock.release()
    assert not lock.held and ch.row is None
    assert "ALTER TABLE loom._migration_lock DELETE" in ch.commands[-1][0]
    lock.release()  # a second release sends nothing
    assert len([c for c in ch.commands if " DELETE " in c[0]]) == 1


def test_contender_waits_on_a_duplicate_key_until_the_holder_releases() -> None:
    clock, out_a, out_b = Clock(), [], []
    ch = FakeKeeper(clock)
    a = _lock(ch, clock, "web:1:aa", out_a)
    assert a.acquire() == "held"
    b = _lock(ch, clock, "workers:1:bb", out_b)
    original_sleep = b.sleep
    naps: list[float] = []

    def sleep(s: float) -> None:
        naps.append(s)
        original_sleep(s)
        if len(naps) == 3:
            a.release()

    b.sleep = sleep
    assert b.acquire() == "held"
    assert b.waited_s == pytest.approx(1.0 + 1.5 + 2.25)
    assert ch.row is not None and ch.row["holder"] == "workers:1:bb"
    assert sum("held by web:1:aa" in line for line in out_b) == 1  # announced once


def test_contender_times_out_behind_a_live_holder_and_changes_nothing() -> None:
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    ch.seed("busy:9:zz", renewed_s=0.0)
    me = _lock(ch, clock, "web:2:cc", out, wait_s=20.0)
    assert me.acquire() == "timeout"
    assert not me.held and me.waited_s >= 20.0
    me.release()
    assert ch.row is not None and ch.row["holder"] == "busy:9:zz"
    assert not any(" DELETE " in sql for sql, _ in ch.commands)


def test_a_stale_holder_is_taken_over_by_a_conditional_update() -> None:
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    ch.seed("dead:9:zz", renewed_s=-400.0, ttl_s=300)
    me = _lock(ch, clock, "web:1:aa", out)
    assert me.acquire() == "held" and me.waited_s == 0.0
    sql, p = next(c for c in ch.commands if " UPDATE holder = " in c[0])
    assert p["old"] == "dead:9:zz"
    assert "holder = {old:String}" in sql and "< now64(3)" in sql
    assert ch.row is not None and ch.row["holder"] == "web:1:aa"
    assert any("took over from dead:9:zz" in line for line in out)


def test_a_takeover_that_loses_the_version_race_waits_for_the_winner() -> None:
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    ch.seed("dead:9:zz", renewed_s=-400.0, ttl_s=300)

    def rival_wins() -> None:
        ch.row = {
            "holder": "rival:1:rr",
            "acquired_at": clock.now,
            "renewed_at": clock.now,
            "ttl_s": 300,
        }

    ch.before_update.append(rival_wins)
    me = _lock(ch, clock, "web:1:aa", out, wait_s=5.0)
    assert me.acquire() == "timeout"
    assert ch.row["holder"] == "rival:1:rr"
    assert any("held by rival:1:rr" in line for line in out)


def test_renewal_is_conditional_on_holder_and_detects_a_takeover() -> None:
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    me = _lock(ch, clock, "web:1:aa", out, ttl_s=30)
    assert me.acquire() == "held"
    n = len(ch.commands)
    clock.sleep(5)
    me.renew_if_due()
    assert len(ch.commands) == n  # not due yet
    clock.sleep(6)
    me.renew_if_due()
    sql, p = ch.commands[-1]
    assert " UPDATE renewed_at = " in sql and p["me"] == "web:1:aa"
    assert ch.row["renewed_at"] == clock.now and me.held
    # Someone took the key while this holder was not renewing.
    ch.row["holder"] = "other:2:oo"
    clock.sleep(11)
    me.renew_if_due()
    assert not me.held
    assert ch.row["holder"] == "other:2:oo"  # the conditional update changed nothing
    assert any("lost to other:2:oo" in line for line in out)
    me.release()
    assert ch.row is not None  # a lost lock is not deleted


def test_heartbeat_renews_from_its_own_connection() -> None:
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    me = _lock(ch, clock, "web:1:aa", out, ttl_s=3)
    assert me.acquire() == "held"
    beats: list[str] = []

    class Beat:
        def command(self, sql, parameters=None, settings=None):
            beats.append(sql)
            return ch.command(sql, parameters=parameters, settings=settings)

        def query(self, sql, parameters=None, settings=None):
            return ch.query(sql, parameters=parameters, settings=settings)

    me.start_heartbeat(Beat)
    import time

    deadline = time.monotonic() + 5
    while not beats and time.monotonic() < deadline:
        time.sleep(0.05)
    me.release()
    assert beats and " UPDATE renewed_at = " in beats[0]
    assert ch.row is None


def test_bootstrap_reports_a_server_without_keeper() -> None:
    clock = Clock()
    assert bootstrap_keeper_lock(FakeKeeper(clock)) is None
    for msg in (
        "Code: 999. DB::Exception: Cannot get ZooKeeper. (KEEPER_EXCEPTION)",
        "Code: 139. DB::Exception: KeeperMap is disabled. (NO_ELEMENTS_IN_CONFIG)",
        "Code: 36. DB::Exception: KeeperMap is disabled. (BAD_ARGUMENTS)",
    ):
        why = bootstrap_keeper_lock(FakeKeeper(clock, create_error=msg))
        assert why is not None and "no Keeper" in why
    other = bootstrap_keeper_lock(
        FakeKeeper(clock, create_error="Code: 497. not enough privileges")
    )
    assert other is not None and "497" in other


# -- the factory ------------------------------------------------------------------


@pytest.fixture
def lease_on(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "boot_migrate_lease_wait_s", 5.0)
    monkeypatch.setattr(mig, "client", lambda: (_ for _ in ()).throw(RuntimeError("no dial")))


def test_factory_uses_the_keeper_lock_when_the_table_is_created(lease_on, capsys) -> None:
    ch = FakeKeeper(Clock())
    lease = mig._acquire_lease(ch)
    assert isinstance(lease, KeeperLock) and lease.held
    out = capsys.readouterr().out
    assert "migration lock: KeeperMap loom._migration_lock" in out
    assert "using the lease table" not in out
    lease.release()


@pytest.mark.parametrize(
    "where", ["create", "insert"], ids=["no-keeper-table", "keeper-refuses-insert"]
)
def test_factory_falls_back_to_the_lease_table(lease_on, monkeypatch, capsys, where) -> None:
    ch = FakeKeeper(Clock())
    err = "Code: 999. DB::Exception: Cannot get ZooKeeper. (KEEPER_EXCEPTION)"
    if where == "create":
        ch.create_error = err
    else:
        ch.insert_error = err
    acquired: list[str] = []
    monkeypatch.setattr(
        MigrationLease, "acquire", lambda self: acquired.append("rmt") or "unleased"
    )
    lease = mig._acquire_lease(ch)
    assert isinstance(lease, MigrationLease) and acquired == ["rmt"]
    out = capsys.readouterr().out
    assert out.count("using the lease table loom._migration_lease") == 1
    assert ch.row is None


def test_apply_stops_when_the_lock_is_lost_between_files(monkeypatch, tmp_path, capsys) -> None:
    class Lost:
        outcome, held, waited_s, holder = "held", True, 0.0, "me"

        def renew_if_due(self) -> None:
            self.held = False

        def release(self) -> None:
            pass

    d = tmp_path
    for n in ("1998_a.sql", "1999_b.sql"):
        (d / n).write_text("CREATE TABLE IF NOT EXISTS loom.x (a UInt8) ENGINE = Memory;\n")
    ran: list[str] = []
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_codec_supported", lambda ch: False)
    monkeypatch.setattr(mig, "_sync_settings_accepted", lambda ch: frozenset())
    monkeypatch.setattr(mig, "_run_file", lambda ch, f, **kw: ran.append(f.name))
    monkeypatch.setattr(mig, "_record", lambda ch, name, cs: None)
    files = sorted_migrations(d.glob("*.sql"))
    assert mig._apply_pending(object(), files, dry=False, lease=Lost()) == 1  # type: ignore[arg-type]
    assert ran == ["1998_a.sql"]
    assert "migration lock lost" in capsys.readouterr().err
