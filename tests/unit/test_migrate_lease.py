"""The boot-time migration step is
serialised across containers by a lease row in `loom._migration_lease`.

CH-free. `FakeCH` keeps one row per holder (the ReplacingMergeTree collapse),
stamps `acquired_at` / `renewed_at` from a controllable clock and decides
liveness the way the server's `now64(3)` would; `before_query` hooks inject a
sibling's insert between one contender's insert and its read-back."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.core.schema import migrate as mig
from server.core.schema.migrate_lease import (
    LEASE_NAME,
    LEASE_TABLE,
    ROWS_SETTINGS,
    LeaseConfig,
    LeaseRow,
    MigrationLease,
    describe_error,
    winner,
)

T0 = datetime(2026, 9, 16, 15, 11, 0)


class Clock:
    """One monotonic clock shared by the fake server and every contender."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s

    @property
    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.t)


class FakeCH:
    def __init__(self, clock: Clock, *, missing: bool = False) -> None:
        self.clock = clock
        self.missing = missing
        self.rows: dict[str, dict] = {}
        self.inserts: list[tuple[list[str], list]] = []
        self.queries = 0
        self.before_query: list[Callable[[], None]] = []

    def seed(
        self,
        holder: str,
        *,
        acquired_s: float,
        renewed_s: float,
        ttl_s: int = 300,
        released: bool = False,
    ) -> None:
        self.rows[holder] = {
            "holder": holder,
            "acquired_at": T0 + timedelta(seconds=acquired_s),
            "renewed_at": T0 + timedelta(seconds=renewed_s),
            "ttl_s": ttl_s,
            "released": released,
            "version": 0,
        }

    def insert(self, table: str, rows: list, column_names: list[str]) -> None:
        if self.missing:
            raise RuntimeError(
                "Code: 60. DB::Exception: Table loom._migration_lease does not exist"
            )
        assert table == LEASE_TABLE
        for r in rows:
            d = dict(zip(column_names, r, strict=True))
            assert d["name"] == LEASE_NAME
            d.setdefault("acquired_at", self.clock.now)
            # ClickHouse stores the instant and reads it back naive UTC.
            d["acquired_at"] = d["acquired_at"].replace(tzinfo=None)
            d["renewed_at"] = self.clock.now
            prev = self.rows.get(d["holder"])
            if prev is None or d["version"] >= prev["version"]:
                self.rows[d["holder"]] = d
            self.inserts.append((list(column_names), list(r)))

    def query(self, sql: str, parameters=None, settings=None):
        if self.missing:
            raise RuntimeError(
                "Code: 60. DB::Exception: Table loom._migration_lease does not exist"
            )
        assert "FINAL" in sql and LEASE_TABLE in sql
        assert settings == ROWS_SETTINGS
        assert parameters == {"name": LEASE_NAME}
        self.queries += 1
        if self.before_query:  # one hook per read-back, in order
            self.before_query.pop(0)()
        now = self.clock.now
        out = [
            (
                d["holder"],
                d["acquired_at"],
                d["renewed_at"],
                d["ttl_s"],
                d["released"],
                d["renewed_at"] + timedelta(seconds=d["ttl_s"]) > now,
            )
            for d in self.rows.values()
        ]
        return SimpleNamespace(result_rows=out)


def _lease(ch: FakeCH, clock: Clock, holder: str, out: list[str], **cfg) -> MigrationLease:
    conf = LeaseConfig(**{"settle_s": 1.0, "poll_s": 1.0, "poll_max_s": 4.0, "wait_s": 60.0, **cfg})
    return MigrationLease(
        ch,
        cfg=conf,
        holder=holder,
        build_commit="abc123",
        sleep=clock.sleep,
        clock=clock.monotonic,
        out=out.append,
    )


# -- the protocol ---------------------------------------------------------------


def test_lone_contender_holds_after_the_settle_and_releases() -> None:
    clock, out = Clock(), []
    ch = FakeCH(clock)
    lease = _lease(ch, clock, "web:1:aa", out)
    assert lease.acquire() == "held"
    assert lease.held and lease.waited_s == pytest.approx(1.0)  # the settle only
    assert ch.queries == 1
    cols, row = ch.inserts[0]
    assert cols == ["name", "holder", "ttl_s", "released", "build_commit", "version"]
    assert row[:5] == [LEASE_NAME, "web:1:aa", 300, False, "abc123"]
    lease.release()
    assert not lease.held
    cols, row = ch.inserts[-1]
    assert "acquired_at" in cols and row[cols.index("released")] is True
    # the queue position is kept, sent as the UTC instant it was read as
    assert row[cols.index("acquired_at")] == T0.replace(tzinfo=UTC)
    assert ch.rows["web:1:aa"]["released"] is True
    assert winner([]) is None


def test_second_contender_waits_until_the_holder_releases() -> None:
    clock, out_a, out_b = Clock(), [], []
    ch = FakeCH(clock)
    a = _lease(ch, clock, "web:1:aa", out_a)
    assert a.acquire() == "held"
    b = _lease(ch, clock, "workers:1:bb", out_b)
    # a releases while b is on its third read-back.
    ch.before_query.extend([lambda: None, lambda: None, a.release])
    assert b.acquire() == "held"
    assert b.waited_s >= 3.0
    assert any("held by web:1:aa" in line and "waiting" in line for line in out_b)
    assert ch.rows["web:1:aa"]["released"] is True and not a.held


def test_an_earlier_row_that_lands_during_the_settle_wins() -> None:
    """The FIFO rule: a sibling whose insert was acknowledged a moment earlier
    on another replica is in the read-back after the settle and outranks a
    contender that inserted later, even though this contender read first."""
    clock, out = Clock(), []
    ch = FakeCH(clock)
    me = _lease(ch, clock, "web:1:aa", out)

    def sibling_lands() -> None:
        ch.seed("workers:1:bb", acquired_s=-0.4, renewed_s=-0.4)

    def sibling_releases() -> None:
        ch.rows["workers:1:bb"]["released"] = True

    ch.before_query.extend([sibling_lands, sibling_releases])
    assert me.acquire() == "held"
    assert ch.queries == 2
    assert any("held by workers:1:bb" in line for line in out)


def test_tie_on_the_millisecond_is_broken_by_the_holder_string() -> None:
    rows = [
        LeaseRow("z", T0, T0, 300, False, True),
        LeaseRow("a", T0, T0, 300, False, True),
        LeaseRow("0-released", T0 - timedelta(seconds=5), T0, 300, True, True),
        LeaseRow("0-expired", T0 - timedelta(seconds=9), T0, 300, False, False),
    ]
    top = winner(rows)
    assert top is not None and top.holder == "a"


def test_a_crashed_holder_is_replaced_once_its_ttl_passes() -> None:
    clock, out = Clock(), []
    ch = FakeCH(clock)
    ch.seed("dead:9:zz", acquired_s=-400, renewed_s=-400, ttl_s=300)  # never released
    me = _lease(ch, clock, "web:1:aa", out)
    assert me.acquire() == "held"
    assert me.waited_s == pytest.approx(1.0)
    # …but a live one is waited for.
    ch2 = FakeCH(clock)
    ch2.seed("busy:9:zz", acquired_s=-100, renewed_s=-100, ttl_s=300)
    other = _lease(ch2, clock, "web:2:cc", out, wait_s=20.0)
    assert other.acquire() == "timeout"
    assert not other.held and other.waited_s >= 20.0
    other.release()  # the timed-out contender's own row is released too
    assert ch2.rows["web:2:cc"]["released"] is True


def test_missing_lease_table_runs_unleased_and_release_is_a_no_op() -> None:
    clock, out = Clock(), []
    ch = FakeCH(clock, missing=True)
    me = _lease(ch, clock, "web:1:aa", out)
    assert me.acquire() == "unleased"
    assert not me.held
    assert any("not applied here yet" in line for line in out)
    me.release()
    assert ch.inserts == []
    assert describe_error(RuntimeError("Code: 60. missing")).startswith(
        "table loom._migration_lease absent"
    )
    assert describe_error(ValueError("boom")) == "ValueError: boom"


def test_renewal_is_due_after_a_third_of_the_ttl_and_keeps_the_position() -> None:
    clock, out = Clock(), []
    ch = FakeCH(clock)
    me = _lease(ch, clock, "web:1:aa", out, ttl_s=300)
    assert me.acquire() == "held"
    n = len(ch.inserts)
    clock.sleep(50)
    me.renew_if_due()
    assert len(ch.inserts) == n  # not yet
    clock.sleep(60)
    me.renew_if_due()
    assert len(ch.inserts) == n + 1
    cols, row = ch.inserts[-1]
    assert row[cols.index("acquired_at")] == T0.replace(tzinfo=UTC)
    assert row[cols.index("released")] is False
    assert ch.rows["web:1:aa"]["renewed_at"] == clock.now
    # A waiting contender renews too, so it does not expire in the queue.
    waiter_out: list[str] = []
    ch.seed("holder:1:hh", acquired_s=-10, renewed_s=clock.t)
    w = _lease(ch, clock, "web:3:ww", waiter_out, ttl_s=30, poll_s=4.0, poll_max_s=4.0, wait_s=40.0)
    assert w.acquire() == "timeout"
    renewals = [
        r for c, r in ch.inserts if r[1] == "web:3:ww" and "acquired_at" in c and r[3] is False
    ]
    assert renewals, "the waiter never renewed its own row"


# -- the runner around it ---------------------------------------------------------


class FakeLease:
    def __init__(self, outcome: str, waited_s: float = 0.0) -> None:
        self.outcome = outcome
        self.held = outcome == "held"
        self.waited_s = waited_s
        self.holder = "fake:1:ff"
        self.renewals = 0
        self.released = False

    def renew_if_due(self) -> None:
        self.renewals += 1

    def release(self) -> None:
        self.released = True
        self.held = False


def _runner(monkeypatch, tmp_path: Path, *, applied_reads: list[dict[str, str]], lease):
    d = tmp_path / "migrations"
    d.mkdir(parents=True)
    (d / "1999_itest.sql").write_text(
        "CREATE TABLE IF NOT EXISTS loom.x (a UInt8) ENGINE = MergeTree ORDER BY a;\n"
    )
    applied: list[int] = []
    recorded: list[str] = []
    reads = list(applied_reads)
    monkeypatch.setattr(mig, "client", lambda: object())
    monkeypatch.setattr(mig, "_applied", lambda ch: reads.pop(0) if len(reads) > 1 else reads[0])
    monkeypatch.setattr(mig, "_apply_file", lambda ch, f, **kw: applied.append(1))
    monkeypatch.setattr(mig, "_record", lambda ch, name, cs: recorded.append(name))
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_codec_supported", lambda ch: False)
    monkeypatch.setattr(mig, "_acquire_lease", lambda ch: lease)
    return d, applied, recorded


def test_main_re_reads_the_ledger_under_the_lease_and_applies_nothing_twice(
    monkeypatch, tmp_path, capsys
) -> None:
    """The waiter's view: pending before the lease, applied by the sibling
    once it holds — nothing runs, one ledger row, the lease is released."""
    lease = FakeLease("held", waited_s=3.2)
    cs = mig._checksum(
        "CREATE TABLE IF NOT EXISTS loom.x (a UInt8) ENGINE = MergeTree ORDER BY a;\n"
    )
    d, applied, recorded = _runner(
        monkeypatch, tmp_path, applied_reads=[{}, {"1999_itest.sql": cs}], lease=lease
    )
    assert mig.main(["--migrations-dir", str(d)]) == 0
    assert applied == [] and recorded == []
    assert lease.released
    out = capsys.readouterr().out
    assert "1 migration(s) to apply" in out
    assert "applied by another process while this one waited 3.2 s" in out


def test_main_applies_once_when_it_holds_the_lease_and_renews_between_files(
    monkeypatch, tmp_path, capsys
) -> None:
    resets: list[bool] = []
    monkeypatch.setattr("server.core.schema.status.reset_cache", lambda: resets.append(True))
    lease = FakeLease("held")
    d, applied, recorded = _runner(monkeypatch, tmp_path, applied_reads=[{}], lease=lease)
    assert mig.main(["--migrations-dir", str(d)]) == 0
    assert applied == [1] and recorded == ["1999_itest.sql"]
    assert lease.renewals == 1 and lease.released
    assert resets == [True]
    assert "done — 1 applied" in capsys.readouterr().out


def test_main_gives_up_on_a_lease_timeout_and_releases_its_row(
    monkeypatch, tmp_path, capsys
) -> None:
    lease = FakeLease("timeout", waited_s=900.0)
    d, applied, recorded = _runner(monkeypatch, tmp_path, applied_reads=[{}], lease=lease)
    assert mig.main(["--migrations-dir", str(d)]) == 1
    assert applied == [] and recorded == [] and lease.released
    assert "still held by another process after 900 s" in capsys.readouterr().err


def test_main_runs_unleased_when_the_table_is_absent(monkeypatch, tmp_path) -> None:
    lease = FakeLease("unleased")
    d, applied, recorded = _runner(monkeypatch, tmp_path, applied_reads=[{}], lease=lease)
    assert mig.main(["--migrations-dir", str(d)]) == 0
    assert applied == [1] and recorded == ["1999_itest.sql"] and lease.released


def test_dry_run_takes_no_lease(monkeypatch, tmp_path, capsys) -> None:
    def boom(ch):
        raise AssertionError("dry-run must not touch the lease")

    d, applied, recorded = _runner(monkeypatch, tmp_path, applied_reads=[{}], lease=None)
    monkeypatch.setattr(mig, "_acquire_lease", boom)
    assert mig.main(["--dry-run", "--migrations-dir", str(d)]) == 0
    assert recorded == []  # the patched _apply_file is called with dry=True; nothing is recorded
    assert "nothing applied" in capsys.readouterr().out


def test_lease_config_reads_settings(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "boot_migrate_lease_ttl_s", 120)
    monkeypatch.setattr(settings, "boot_migrate_lease_wait_s", 30.0)
    monkeypatch.setattr(settings, "boot_migrate_lease_settle_s", 0.5)
    cfg = mig._lease_config()
    assert cfg == LeaseConfig(ttl_s=120, wait_s=30.0, settle_s=0.5)


def test_lease_settings_defaults() -> None:
    from server.settings import Settings

    f = Settings.model_fields
    assert f["boot_migrate_lease_ttl_s"].default == 300
    assert f["boot_migrate_lease_wait_s"].default == 900.0
    assert f["boot_migrate_lease_settle_s"].default == 1.0


# -- the belt ---------------------------------------------------------------------


class LedgerCH:
    def __init__(self, existing: str | None) -> None:
        self.existing = existing
        self.inserted: list = []
        self.query_settings = None

    def query(self, sql, parameters=None, settings=None):
        assert "loom._migrations FINAL" in sql and parameters == {"f": "1999_itest.sql"}
        self.query_settings = settings
        return SimpleNamespace(result_rows=[(self.existing,)] if self.existing else [])

    def insert(self, table, rows, column_names):
        self.inserted.append((table, rows, column_names))


def test_record_skips_a_filename_a_sibling_already_recorded(capsys) -> None:
    ch = LedgerCH("57bb565a79b9")
    mig._record(ch, "1999_itest.sql", "0" * 64)
    assert ch.inserted == []
    assert ch.query_settings == {"select_sequential_consistency": 1}
    assert "already holds 1999_itest.sql (applied_by 57bb565a79b9)" in capsys.readouterr().out


def test_record_inserts_when_the_ledger_lacks_the_filename() -> None:
    ch = LedgerCH(None)
    mig._record(ch, "1999_itest.sql", "0" * 64)
    assert len(ch.inserted) == 1
    table, rows, cols = ch.inserted[0]
    assert table == "loom._migrations" and cols == ["filename", "checksum", "applied_by"]
    assert rows[0][:2] == ["1999_itest.sql", "0" * 64]


def test_record_belt_fails_open_when_the_ledger_read_fails() -> None:
    class Broken(LedgerCH):
        def query(self, *a, **k):
            raise RuntimeError("Code: 60. no ledger yet")

    ch = Broken(None)
    mig._record(ch, "1999_itest.sql", "0" * 64)
    assert len(ch.inserted) == 1


def test_a_pending_lease_migration_is_bootstrapped_before_the_lease_is_taken(
    monkeypatch, tmp_path, capsys
) -> None:
    """The runner creates the lease table before taking the lease (unrecorded)
    so a fresh replay is itself leased, then applies the file in order and
    records it once."""
    lease = FakeLease("held")
    d, applied, recorded = _runner(monkeypatch, tmp_path, applied_reads=[{}], lease=lease)
    (d / mig.LEASE_MIGRATION).write_text(
        "CREATE TABLE IF NOT EXISTS loom._migration_lease (a UInt8) ENGINE = MergeTree ORDER BY a;\n"
    )
    order: list[str] = []
    monkeypatch.setattr(mig, "_apply_file", lambda ch, f, **kw: order.append(f.name))

    def acquire(ch):
        order.append("<acquire>")
        return lease

    monkeypatch.setattr(mig, "_acquire_lease", acquire)
    assert mig.main(["--migrations-dir", str(d)]) == 0
    assert order == [
        mig.LEASE_MIGRATION,  # bootstrap, unrecorded
        "<acquire>",
        mig.LEASE_MIGRATION,  # in numeric order, recorded
        "1999_itest.sql",
    ]
    assert recorded == [mig.LEASE_MIGRATION, "1999_itest.sql"]
    assert "creating the lease table first" in capsys.readouterr().out


def test_the_real_lease_migration_creates_the_lease_table() -> None:
    real = Path(__file__).resolve().parents[2] / "schemas" / "migrations" / mig.LEASE_MIGRATION
    assert f"CREATE TABLE IF NOT EXISTS {LEASE_TABLE}" in real.read_text()


def test_a_server_that_never_shows_my_row_makes_the_run_unleased_not_a_15_minute_wait() -> None:
    """Sequential consistency makes my acknowledged insert visible; a server
    (or a bare fake) that never shows it must not hold the boot for wait_s."""
    clock, out = Clock(), []
    ch = FakeCH(clock)
    real_query = ch.query

    def blind(sql, parameters=None, settings=None):
        real_query(sql, parameters=parameters, settings=settings)
        return SimpleNamespace(result_rows=[])

    ch.query = blind  # type: ignore[method-assign]
    me = _lease(ch, clock, "web:1:aa", out, wait_s=900.0)
    assert me.acquire() == "unleased"
    assert ch.queries == 3 and clock.t < 10
    assert any("own lease row not visible after 3 read-backs" in line for line in out)
    me.release()  # the inserted row is released so no sibling waits on it
    assert ch.rows["web:1:aa"]["released"] is True


def test_a_holder_whose_row_expired_and_was_taken_over_does_not_renew() -> None:
    """One statement outlasted the TTL: the holder's row expired and a
    contender took the lease. The holder's renewal must not revive its row
    (the earlier acquired_at would make it the winner again, so both would
    apply); it reports the lease lost instead."""
    clock, out_a, out_b = Clock(), [], []
    ch = FakeCH(clock)
    a = _lease(ch, clock, "web:1:aa", out_a, ttl_s=300)
    assert a.acquire() == "held"
    clock.sleep(400)  # one long mutation wait, no renewal
    b = _lease(ch, clock, "workers:1:bb", out_b, ttl_s=300)
    assert b.acquire() == "held"
    n = len(ch.inserts)
    a.renew_if_due()
    assert not a.held and a.outcome == "held"  # the runner's "lock lost" test
    assert len(ch.inserts) == n  # a's row stays expired
    assert any("now held by workers:1:bb" in line for line in out_a)
    assert (
        winner(
            [
                LeaseRow(
                    d["holder"],
                    d["acquired_at"],
                    d["renewed_at"],
                    d["ttl_s"],
                    d["released"],
                    d["renewed_at"] + timedelta(seconds=d["ttl_s"]) > clock.now,
                )
                for d in ch.rows.values()
            ]
        ).holder
        == "workers:1:bb"
    )


def test_an_expired_holder_nobody_took_over_from_renews() -> None:
    clock, out = Clock(), []
    ch = FakeCH(clock)
    a = _lease(ch, clock, "web:1:aa", out, ttl_s=300)
    assert a.acquire() == "held"
    clock.sleep(400)
    n = len(ch.inserts)
    a.renew_if_due()
    assert a.held and len(ch.inserts) == n + 1


def test_a_failed_read_back_after_my_row_landed_waits_instead_of_running_unleased() -> None:
    """A live holder is applying; one of my read-backs fails. That failure is
    no evidence the holder is gone, so I keep waiting (and take the lease once
    it releases) instead of applying beside it."""
    clock, out_a, out_b = Clock(), [], []
    ch = FakeCH(clock)
    a = _lease(ch, clock, "web:1:aa", out_a)
    assert a.acquire() == "held"
    real_query = ch.query
    calls = {"n": 0}

    def flaky(sql, parameters=None, settings=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Code: 209. DB::Exception: Timeout exceeded while reading")
        if calls["n"] == 4:
            a.release()
        return real_query(sql, parameters=parameters, settings=settings)

    ch.query = flaky  # type: ignore[method-assign]
    b = _lease(ch, clock, "workers:1:bb", out_b)
    assert b.acquire() == "held"
    assert any("read-back failed" in line for line in out_b)
    assert not any("running without it" in line for line in out_b)


def test_read_backs_that_keep_failing_time_out() -> None:
    clock, out = Clock(), []
    ch = FakeCH(clock)
    ch.seed("busy:9:zz", acquired_s=-10, renewed_s=-10)
    real_query = ch.query
    calls = {"n": 0}

    def failing(sql, parameters=None, settings=None):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("Code: 159. DB::Exception: Timeout exceeded")
        return real_query(sql, parameters=parameters, settings=settings)

    ch.query = failing  # type: ignore[method-assign]
    me = _lease(ch, clock, "web:2:cc", out, wait_s=20.0)
    assert me.acquire() == "timeout"
    assert not me.held and me.waited_s >= 20.0


def test_a_failed_holder_check_after_the_ttl_counts_the_lease_lost() -> None:
    """The holder cannot read the rows and has not renewed for a whole TTL:
    its row may have expired and been taken, so it must not re-insert (that
    would revive the earlier acquired_at beside the new holder)."""
    clock, out = Clock(), []
    ch = FakeCH(clock)
    a = _lease(ch, clock, "web:1:aa", out, ttl_s=300)
    assert a.acquire() == "held"

    def down(sql, parameters=None, settings=None):
        raise RuntimeError("Code: 210. DB::NetException: Connection refused")

    ch.query = down  # type: ignore[method-assign]
    clock.sleep(150)  # past a third of the TTL, inside the TTL: renewing is safe
    n = len(ch.inserts)
    a.renew_if_due()
    assert a.held and len(ch.inserts) == n + 1
    clock.sleep(301)
    n = len(ch.inserts)
    a.renew_if_due()
    assert not a.held and a.outcome == "held"
    assert len(ch.inserts) == n
