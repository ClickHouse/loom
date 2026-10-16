"""Two restores into one scratch database at once, on live ClickHouse. Both
runs inspect the empty target before either claims it; the restore lock then
serializes them, and the second judges the target as the first left it. Two
orgs' backups both land; one backup restored twice lands once, and the second
run refuses instead of reporting a restore whose rows the first run's shadow.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.integration._resources import unique_org

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.usefixtures("ch_target_required")

TABLE = "memory_exposures"


class _Paused:
    """The real client, with each run's first lock attempt held until both
    runs have inspected the target and reach theirs."""

    def __init__(self, client: Any, barrier: threading.Barrier) -> None:
        self._client, self._barrier, self._met = client, barrier, False

    def command(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if "_loom_restore_lock_" in sql and sql.startswith("CREATE") and not self._met:
            self._met = True
            self._barrier.wait()
        return self._client.command(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _concurrently(
    monkeypatch: pytest.MonkeyPatch, ch_client: Any, dirs: list[Path], db: str
) -> list[int]:
    from dev import restore_loom
    from dev._ch_target import CHTarget

    ch_client.command(f"CREATE TABLE `{db}`.`{TABLE}` AS loom.`{TABLE}`")
    barrier = threading.Barrier(2, timeout=60)
    real_client = CHTarget.client
    monkeypatch.setattr(
        CHTarget, "client", lambda self, **kw: _Paused(real_client(self, **kw), barrier)
    )
    with ThreadPoolExecutor(2) as pool:
        runs = [
            pool.submit(restore_loom.main, ["--in-dir", str(d), "--database", db]) for d in dirs
        ]
        codes = [r.result() for r in runs]
    left = ch_client.query(
        "SELECT name FROM system.tables WHERE database = {db:String} AND name != {t:String}",
        parameters={"db": db, "t": TABLE},
    ).result_rows
    assert left == []  # each run dropped its own staging table
    lock = ch_client.query(
        "SELECT count() FROM system.tables WHERE database = 'default' AND name = {t:String}",
        parameters={"t": f"_loom_restore_lock_{db}"},
    ).result_rows
    assert lock[0][0] == 0
    return codes


def test_two_restores_into_one_database_keep_both_rows(
    tmp_path: Path, ch_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dev import backup_loom

    orgs = [unique_org("restore-a"), unique_org("restore-b")]
    ch_client.insert(
        f"loom.{TABLE}",
        [[org, "ns", "memory.search", ["k"]] for org in orgs],
        column_names=["org", "namespace", "operation", "returned_keys"],
    )
    dirs = [tmp_path / org for org in orgs]
    for org, out in zip(orgs, dirs, strict=True):
        argv = ["--out-dir", str(out), "--org", org, "--tables", TABLE]
        assert backup_loom.main(argv) == 0

    db = f"it_restore_{secrets.token_hex(4)}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    try:
        assert _concurrently(monkeypatch, ch_client, dirs, db) == [0, 0]
        rows = ch_client.query(f"SELECT org FROM `{db}`.`{TABLE}` ORDER BY org").result_rows
        assert [r[0] for r in rows] == sorted(orgs)
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS `{db}` SYNC")


def test_one_backup_restored_twice_at_once_lands_once(
    tmp_path: Path, ch_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dev import backup_loom

    org = unique_org("restore-twice")
    ch_client.insert(
        f"loom.{TABLE}",
        [[org, "ns", "memory.search", ["k"]]],
        column_names=["org", "namespace", "operation", "returned_keys"],
    )
    out = tmp_path / org
    assert backup_loom.main(["--out-dir", str(out), "--org", org, "--tables", TABLE]) == 0
    db = f"it_restore_{secrets.token_hex(4)}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    try:
        # The second run sees the first's rows in scope: --into-nonempty was not given.
        assert sorted(_concurrently(monkeypatch, ch_client, [out, out], db)) == [0, 2]
        rows = ch_client.query(f"SELECT count() FROM `{db}`.`{TABLE}`").result_rows
        assert rows[0][0] == 1
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS `{db}` SYNC")
