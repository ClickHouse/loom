"""The migration lease, against a live ClickHouse: two
`server.core.schema.migrate.main` runs that start together — no shared file lock, as two
containers of one colour — apply a NON-idempotent migration once.

One applier, one ledger row: the throwaway migration creates a
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

from server.core.leases import LOCK_TABLE

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

    ledger_rows = ch_client.query(
        "SELECT count() FROM system.tables WHERE database = 'loom' AND name = '_migrations'"
    ).result_rows
    assert ledger_rows and ledger_rows[0][0] == 1, "000_migrations.sql not applied on this service"

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

        # One applier; the other applied nothing. It waited on the lock (and
        # re-read the ledger under it), or, started after the applier had
        # finished, found nothing pending: the barrier releases both, the
        # imports and connects that follow are not timed.
        appliers = [o for o in outs if f"→ {name}" in o and f"✓ {name}" in o]
        others = [o for o in outs if o not in appliers]
        assert len(appliers) == 1 and len(others) == 1, outs
        assert "no pending migrations" in others[0], others[0]

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

        # Both contenders released: the KeeperMap key is gone.
        assert all("migration lock: KeeperMap" in o for o in outs), outs
        held = ch_client.query(
            f"SELECT holder FROM {LOCK_TABLE} WHERE name = 'boot_migrate'"
        ).result_rows
        assert held == [], held
    finally:
        for p in procs:
            if p.is_alive():
                p.kill()
        ch_client.command(f"DROP TABLE IF EXISTS {table}")
        ch_client.command(
            "ALTER TABLE loom._migrations DELETE WHERE filename = {f:String} SETTINGS mutations_sync=2",
            parameters={"f": name},
        )
