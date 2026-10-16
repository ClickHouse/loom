"""The boot-time migration step: applied under the deployment's KeeperMap lock
(`server.core.schema.migrate_lease.KeeperLock`, tested in test_migrate_keeper_lock),
the ledger re-read under it, and stopped on a server without a Keeper."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from server.core.schema import migrate as mig
from server.core.schema.migrate_lease import KeeperRequired, LeaseConfig


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
    monkeypatch.setattr(
        mig, "applied_migrations", lambda ch: reads.pop(0) if len(reads) > 1 else reads[0]
    )
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


def test_main_stops_without_a_keeper_and_names_the_fix(monkeypatch, tmp_path, capsys) -> None:
    def no_keeper(ch):
        raise KeeperRequired("code 139")

    d, applied, recorded = _runner(monkeypatch, tmp_path, applied_reads=[{}], lease=None)
    monkeypatch.setattr(mig, "_acquire_lease", no_keeper)
    assert mig.main(["--migrations-dir", str(d)]) == 1
    assert applied == [] and recorded == []
    assert "Loom needs a ClickHouse Keeper" in capsys.readouterr().err


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
    cfg = mig._lease_config()
    assert cfg == LeaseConfig(ttl_s=120, wait_s=30.0)


def test_lease_settings_defaults() -> None:
    from server.settings import Settings

    f = Settings.model_fields
    assert f["boot_migrate_lease_ttl_s"].default == 300
    assert f["boot_migrate_lease_wait_s"].default == 900.0


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
    """The runner creates its own tables before taking the lock (unrecorded),
    then applies the file in order and records it once."""
    lease = FakeLease("held")
    d, applied, recorded = _runner(monkeypatch, tmp_path, applied_reads=[{}], lease=lease)
    (d / mig.LEASE_MIGRATION).write_text(
        "CREATE TABLE IF NOT EXISTS loom._migrations (a UInt8) ENGINE = MergeTree ORDER BY a;\n"
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
    assert "creating the migrator's tables first" in capsys.readouterr().out
