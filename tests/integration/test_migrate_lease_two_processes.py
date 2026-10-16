"""The migration lease, against a live ClickHouse: two
`server.core.schema.migrate.main` runs that start together — no shared file lock, as two
containers of one colour — apply a NON-idempotent migration once.

One applier, one waiter, one ledger row: the throwaway migration creates a
table and inserts one row into it; a double apply would leave two rows. The
CI job applied every repo migration before the tests run. Either lock
passes: the KeeperMap key where the server has a Keeper, the lease table
otherwise.

Skips cleanly when LOOM_CH_HOST is unset."""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import uuid
from pathlib import Path

import pytest

from server.core.schema.migrate_lease import LEASE_TABLE, LOCK_TABLE

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)


def _run(mig_dir: str, log_path: str, barrier) -> None:
    from server.core.schema import migrate as mig

    sys.stdout = open(log_path, "w")
    barrier.wait(timeout=30)
    rc = mig.main(["--migrations-dir", mig_dir])
    sys.stdout.flush()
    os._exit(rc)


def test_two_concurrent_runs_apply_a_non_idempotent_migration_once(ch_client, tmp_path) -> None:
    from server.core.schema import migrate as mig

    lease_rows = ch_client.query(
        "SELECT count() FROM system.tables WHERE database = 'loom' AND name = '_migration_lease'"
    ).result_rows
    assert lease_rows and lease_rows[0][0] == 1, "000_migrations.sql not applied on this service"

    tag = uuid.uuid4().hex[:8]
    table = f"loom._itest_lease_{tag}"
    name = f"1999_itest_lease_{tag}.sql"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / name).write_text(
        f"CREATE TABLE IF NOT EXISTS {table} (n UInt8) ENGINE = MergeTree ORDER BY n;\n"
        f"INSERT INTO {table} (n) VALUES (1);\n"
    )
    logs = [str(tmp_path / f"run{i}.log") for i in (1, 2)]
    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(2)
    procs = [ctx.Process(target=_run, args=(str(mig_dir), log, barrier)) for log in logs]
    try:
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
        outs = [Path(log).read_text() for log in logs]
        assert [p.exitcode for p in procs] == [0, 0], outs

        # One applier, one waiter.
        appliers = [o for o in outs if f"→ {name}" in o and f"✓ {name}" in o]
        waiters = [o for o in outs if "applied by another process while this one waited" in o]
        assert len(appliers) == 1 and len(waiters) == 1, outs
        # The waiter may have seen the holder ("migration lease: held by …")
        # or, when the applier finished inside the waiter's settle interval,
        # held the lease itself and found the ledger complete: both are the
        # property — one applier, one process that applied nothing.
        assert "migration lease held as" in waiters[0], waiters[0]

        # The non-idempotent statement ran once.
        rows = ch_client.query(f"SELECT count() FROM {table}").result_rows
        assert rows[0][0] == 1, rows

        # One ledger row, raw (no FINAL): the belt and the lease both hold.
        ledger = ch_client.query(
            "SELECT count(), uniqExact(applied_by) FROM loom._migrations WHERE filename = {f:String}",
            parameters={"f": name},
        ).result_rows
        assert ledger[0][0] == 1, ledger
        assert name in mig.applied_migrations(ch_client)

        # Both contenders released: the KeeperMap key is gone, or (no Keeper)
        # every lease row is marked released.
        if any("migration lock: KeeperMap" in o for o in outs):
            assert all("migration lock: KeeperMap" in o for o in outs), outs
            held = ch_client.query(
                f"SELECT holder FROM {LOCK_TABLE} WHERE name = 'boot_migrate'"
            ).result_rows
            assert held == [], held
        else:
            lease = ch_client.query(
                f"SELECT holder, released FROM {LEASE_TABLE} FINAL "
                "WHERE name = 'boot_migrate' AND renewed_at > now64(3) - INTERVAL 5 MINUTE"
            ).result_rows
            assert lease and all(bool(r[1]) for r in lease), lease
    finally:
        for p in procs:
            if p.is_alive():
                p.kill()
        ch_client.command(f"DROP TABLE IF EXISTS {table}")
        ch_client.command(
            "ALTER TABLE loom._migrations DELETE WHERE filename = {f:String} SETTINGS mutations_sync=2",
            parameters={"f": name},
        )
