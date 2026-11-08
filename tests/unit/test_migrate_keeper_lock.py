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

from server.core.leases import LOCK_TABLE
from server.core.schema import migrate as mig
from server.core.schema.journal import sorted_migrations
from server.core.schema.migrate_lease import (
    LEASE_NAME,
    LOCK_DDL,
    KeeperLock,
    KeeperRequired,
    KeeperUnavailable,
    LeaseConfig,
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
    def __init__(
        self, clock: Clock, *, create_error: str | None = None, name: str = LEASE_NAME
    ) -> None:
        self.clock = clock
        self.name = name
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
        assert p.get("name") == self.name
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
        assert LOCK_TABLE in sql and parameters == {"name": self.name}
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


def test_renewals_that_fail_for_half_the_ttl_lose_the_lock() -> None:
    """Stop while the key is still this holder's, before another can take it."""
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    me = _lock(ch, clock, "web:1:aa", out, ttl_s=30)
    assert me.acquire() == "held"
    ch.command = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))  # type: ignore[method-assign]
    clock.sleep(11)
    me.renew_if_due()
    assert me.held and any("renewal failed" in line for line in out)
    clock.sleep(5)
    me.renew_if_due()
    assert not me.held and any("no renewal in time" in line for line in out)


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


def test_bootstrap_refuses_a_server_without_keeper() -> None:
    clock = Clock()
    assert bootstrap_keeper_lock(FakeKeeper(clock)) is None
    for msg in (
        "Code: 139. DB::Exception: KeeperMap is disabled. (NO_ELEMENTS_IN_CONFIG)",
        "Code: 36. DB::Exception: KeeperMap is disabled. (BAD_ARGUMENTS)",
    ):
        with pytest.raises(KeeperRequired, match="Loom needs a ClickHouse Keeper"):
            bootstrap_keeper_lock(FakeKeeper(clock, create_error=msg))
    with pytest.raises(Exception, match="497"):
        bootstrap_keeper_lock(FakeKeeper(clock, create_error="Code: 497. not enough privileges"))


def test_bootstrap_retries_a_failing_keeper_and_never_falls_back_on_it() -> None:
    """A Keeper that is there but failing is not a server without one: retried."""
    clock = Clock()
    ch = FakeKeeper(clock, create_error="Code: 999. Session expired. (KEEPER_EXCEPTION)")
    with pytest.raises(KeeperUnavailable):
        bootstrap_keeper_lock(ch, wait_s=10.0, poll_s=2.0, sleep=clock.sleep, clock=clock.monotonic)
    assert clock.t >= 10.0 and len(ch.commands) == 6
    calls = []

    def heal(s: float) -> None:
        calls.append(s)
        ch.create_error = None

    assert bootstrap_keeper_lock(ch, wait_s=10.0, sleep=heal, clock=clock.monotonic) is None
    assert calls == [2.0]


def test_a_keeper_error_on_the_first_insert_retries_instead_of_falling_back() -> None:
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    ch.insert_error = "Code: 999. Connection loss. (KEEPER_EXCEPTION)"
    me = _lock(ch, clock, "web:1:aa", out, wait_s=5.0)
    assert me.acquire() == "timeout" and not me.held
    ch.insert_error = "Code: 497. not enough privileges"
    assert _lock(ch, clock, "web:1:aa", out).acquire() == "unleased"


def test_a_holder_that_counted_its_key_lost_takes_it_back_at_once() -> None:
    """Missed renewals stop the workers while the key is still this holder's;
    when ClickHouse answers again the key is renewed back, not waited out
    until it is stale (a TTL with no supervisor anywhere)."""
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    me = _lock(ch, clock, "web:1:aa", out, ttl_s=30, wait_s=0.0)
    assert me.acquire() == "held"
    real = ch.command
    ch.command = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))  # type: ignore[method-assign]
    clock.sleep(16)
    me.renew_if_due()
    assert not me.held
    ch.command = real  # type: ignore[method-assign]
    assert me.acquire() == "held" and me.waited_s == 0.0
    assert ch.row["holder"] == "web:1:aa" and ch.row["renewed_at"] == clock.now
    assert not ch._stale()
    assert me.last_renew == clock.t
    # Someone else's live key is not taken back.
    other = _lock(ch, clock, "workers:2:bb", [], ttl_s=30, wait_s=0.0)
    assert other.acquire() == "timeout"


def test_the_renewal_is_stamped_when_it_was_sent() -> None:
    """A renewal that takes seconds to answer counts from its send: the
    server's renewed_at is never earlier than the holder believes."""
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    me = _lock(ch, clock, "web:1:aa", out, ttl_s=30)
    assert me.acquire() == "held"
    clock.sleep(11)
    sent = clock.t
    real = ch.query

    def slow(*a, **k):
        clock.sleep(6)
        return real(*a, **k)

    ch.query = slow  # type: ignore[method-assign]
    me.renew_if_due()
    assert me.held and me.last_renew == sent


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
    "where", ["create", "insert"], ids=["no-keeper-table", "no-keeper-on-insert"]
)
def test_factory_refuses_a_server_without_keeper(lease_on, where) -> None:
    ch = FakeKeeper(Clock())
    err = "Code: 36. DB::Exception: KeeperMap is disabled. (BAD_ARGUMENTS)"
    if where == "create":
        ch.create_error = err
    else:
        ch.insert_error = err
    with pytest.raises(KeeperRequired):
        mig._acquire_lease(ch)
    assert ch.row is None


def test_factory_refused_by_a_keeper_that_is_there_retries(lease_on, monkeypatch, capsys) -> None:
    """A refusal other than "no Keeper" (a privilege) is not a missing Keeper:
    the boot retries."""
    ch = FakeKeeper(Clock())
    ch.insert_error = "Code: 497. DB::Exception: not enough privileges. (ACCESS_DENIED)"
    lease = mig._acquire_lease(ch)
    assert lease.outcome == "timeout" and not lease.held
    assert "ACCESS_DENIED" in capsys.readouterr().err


def test_factory_gives_up_on_a_failing_keeper(lease_on, monkeypatch, capsys) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "boot_migrate_lease_wait_s", 0.0)
    ch = FakeKeeper(Clock(), create_error="Code: 999. Cannot get ZooKeeper. (KEEPER_EXCEPTION)")
    lease = mig._acquire_lease(ch)
    assert lease.outcome == "timeout" and not lease.held
    assert "Cannot get ZooKeeper" in capsys.readouterr().err


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
    monkeypatch.setattr(mig, "_codec_settings", lambda ch: mig.NO_CODEC)
    monkeypatch.setattr(mig, "_sync_settings_accepted", lambda ch: frozenset())
    monkeypatch.setattr(mig, "_run_file", lambda ch, f, **kw: ran.append(f.name))
    monkeypatch.setattr(mig, "_record", lambda ch, name, cs: None)
    files = sorted_migrations(d.glob("*.sql"))
    assert mig._apply_pending(object(), files, dry=False, lease=Lost()) == 1  # type: ignore[arg-type]
    assert ran == ["1998_a.sql"]
    assert "migration lock lost" in capsys.readouterr().err


def test_a_failed_key_release_still_lets_go() -> None:
    clock, out = Clock(), []
    ch = FakeKeeper(clock)
    lock = _lock(ch, clock, "web:1:aa", out)
    assert lock.acquire() == "held"
    original = ch.command

    def refuse_delete(sql: str, parameters=None, settings=None):
        if " DELETE " in sql:
            raise RuntimeError("Code: 210. DB::NetException: Connection refused")
        return original(sql, parameters, settings)

    ch.command = refuse_delete  # type: ignore[method-assign]
    lock.release()  # the key expires by its TTL
    assert not lock.held and any("release failed" in line for line in out)


def test_describe_error_names_a_missing_lock_table() -> None:
    from server.core.schema.migrate_lease import describe_error

    assert describe_error(RuntimeError("Code: 60. UNKNOWN_TABLE")) == f"table {LOCK_TABLE} absent"
    # `error` reaches `/v1/health` without a token: the kind; the operator's lines
    # carry the text.
    assert describe_error(ValueError("x (ch-internal-7:9440)")) == "ValueError"
    from server.core.schema.migrate_lease import operator_error_text

    assert operator_error_text(ValueError("x")) == "ValueError: x"


def test_a_refused_insert_or_takeover_raises_rather_than_counting_as_lost() -> None:
    clock = Clock()
    ch = FakeKeeper(clock)
    ch.insert_error = "Code: 497. not enough privileges"
    me = _lock(ch, clock, "web:1:aa", [])
    with pytest.raises(RuntimeError, match="497"):
        me._try_insert()

    class Refusing(FakeKeeper):
        def command(self, sql: str, parameters=None, settings=None):
            if " UPDATE holder = " in sql:
                raise RuntimeError("Code: 497. not enough privileges")
            return super().command(sql, parameters, settings)

    ch = Refusing(clock)
    ch.seed("dead:1:zz", renewed_s=-1000)
    with pytest.raises(RuntimeError, match="497"):
        _lock(ch, clock, "web:1:aa", [])._try_takeover("dead:1:zz")


def test_a_key_released_between_the_insert_and_the_read_is_a_lost_round() -> None:
    clock = Clock()

    class Vanishing(FakeKeeper):
        reads = 0

        def query(self, sql: str, parameters=None, settings=None):
            self.reads += 1
            if self.reads == 1:  # the holder before the insert
                self.seed("other:1:bb", renewed_s=0)
                return SimpleNamespace(result_rows=[])
            self.row = None  # released before the read after the failed insert
            return super().query(sql, parameters, settings)

    assert _lock(Vanishing(clock), clock, "web:1:aa", [])._attempt() == (False, None)


def test_peek_free_reads_the_key_once() -> None:
    clock = Clock()
    ch = FakeKeeper(clock)
    me = _lock(ch, clock, "web:1:aa", [])
    assert me.peek_free()
    ch.seed("other:1:bb", renewed_s=0)
    assert not me.peek_free()
    ch.seed("web:1:aa", renewed_s=0)
    assert me.peek_free()
    ch.seed("other:1:bb", renewed_s=-1000)
    assert me.peek_free()  # stale


def test_a_lock_not_held_neither_renews_nor_starts_a_heartbeat() -> None:
    clock = Clock()
    ch = FakeKeeper(clock)
    me = _lock(ch, clock, "web:1:aa", [])
    me.renew_if_due()
    me.start_heartbeat(lambda: pytest.fail("no heartbeat for a lock not held"))
    assert ch.commands == []
