"""Boot migrations run one process at a time under LOOM_WEB_WORKERS > 1.

Two migrate calls that overlap in time must not overlap in execution; a
sibling that arrives while the lock is held waits, then runs and sees the
ledger the first one wrote. An unopenable lock path degrades to unserialised
runs rather than refusing to boot."""

from __future__ import annotations

import multiprocessing as mp
import os
import time

from server.boot import run_boot_migrations_if_enabled, run_boot_migrations_serialised


def _worker(lock_path: str, log_path: str, hold_s: float) -> None:
    def migrate(_argv: list[str]) -> int:
        with open(log_path, "a") as f:
            f.write(f"start {os.getpid()} {time.monotonic():.4f}\n")
        time.sleep(hold_s)
        with open(log_path, "a") as f:
            f.write(f"end {os.getpid()} {time.monotonic():.4f}\n")
        return 0

    run_boot_migrations_serialised(migrate, lock_path)


def test_sibling_processes_apply_one_at_a_time(tmp_path) -> None:
    lock = str(tmp_path / "migrate.lock")
    log = str(tmp_path / "log.txt")
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_worker, args=(lock, log, 0.3)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)
        assert p.exitcode == 0
    events = [line.split() for line in open(log).read().splitlines()]
    assert sum(1 for e in events if e[0] == "start") == 4
    # No "start" may fall between another process's "start" and "end".
    open_pid: str | None = None
    for kind, pid, _ts in events:
        if kind == "start":
            assert open_pid is None, f"{pid} started while {open_pid} held the lock"
            open_pid = pid
        else:
            assert open_pid == pid
            open_pid = None


def test_returns_migrate_result_and_releases_lock(tmp_path) -> None:
    lock = str(tmp_path / "m.lock")
    assert run_boot_migrations_serialised(lambda argv: 3, lock) == 3
    assert run_boot_migrations_serialised(lambda argv: 0, lock) == 0  # re-acquirable


def test_unopenable_lock_path_runs_unserialised(tmp_path) -> None:
    bad = str(tmp_path / "no-such-dir" / "m.lock")
    assert run_boot_migrations_serialised(lambda argv: 0, bad) == 0


def test_boot_migrate_switch_skips_the_step_when_off(tmp_path, caplog) -> None:
    """LOOM_BOOT_MIGRATE=false applies nothing and returns 0."""
    calls: list[list[str]] = []

    def migrate(argv: list[str]) -> int:
        calls.append(argv)
        return 7

    lock = str(tmp_path / "lock")
    assert run_boot_migrations_if_enabled(migrate, lock, enabled=True) == 7
    assert calls == [[]]
    with caplog.at_level("WARNING"):
        assert run_boot_migrations_if_enabled(migrate, lock, enabled=False) == 0
    assert calls == [[]]  # not called again
    assert any("boot migrations skipped" in r.message for r in caplog.records)


def test_boot_migrate_defaults_on() -> None:
    from server.settings import Settings

    assert Settings.model_fields["boot_migrate"].default is True


def test_boot_migrate_skip_names_the_pending_files(tmp_path, caplog) -> None:
    """The skip names what the service lacks, in the
    WARNING and in the module's snapshot for /v1/health."""
    import server.boot as boot_mod

    lock = str(tmp_path / "lock")
    with caplog.at_level("WARNING"):
        rc = run_boot_migrations_if_enabled(
            lambda argv: 7,
            lock,
            enabled=False,
            pending=lambda: ["800_namespace_classification.sql", "810_tracker_items_edges.sql"],
        )
    assert rc == 0
    msgs = [r.getMessage() for r in caplog.records if "boot migrations skipped" in r.getMessage()]
    assert msgs and "2 pending" in msgs[-1]
    assert "800_namespace_classification.sql, 810_tracker_items_edges.sql" in msgs[-1]
    assert boot_mod.SKIPPED_PENDING_MIGRATIONS == (
        "800_namespace_classification.sql",
        "810_tracker_items_edges.sql",
    )
    # Nothing pending: the line says so and the snapshot is empty.
    with caplog.at_level("WARNING"):
        assert (
            run_boot_migrations_if_enabled(lambda argv: 7, lock, enabled=False, pending=list) == 0
        )
    assert boot_mod.SKIPPED_PENDING_MIGRATIONS == ()
    assert any("nothing is pending" in r.getMessage() for r in caplog.records)
    # Enabled: the snapshot is reset — the deployment applied everything.
    assert (
        run_boot_migrations_if_enabled(lambda argv: 3, lock, enabled=True, pending=lambda: ["x"])
        == 3
    )
    assert boot_mod.SKIPPED_PENDING_MIGRATIONS == ()


def test_boot_migrate_skip_pending_probe_fails_open(tmp_path, caplog) -> None:
    import server.boot as boot_mod

    def boom() -> list[str]:
        raise RuntimeError("ledger unreachable")

    with caplog.at_level("WARNING"):
        assert (
            run_boot_migrations_if_enabled(
                lambda argv: 7, str(tmp_path / "lock"), enabled=False, pending=boom
            )
            == 0
        )
    assert boot_mod.SKIPPED_PENDING_MIGRATIONS == ()
    assert any("pending-migration check failed" in r.getMessage() for r in caplog.records)
    assert any("boot migrations skipped" in r.getMessage() for r in caplog.records)


def test_pending_names_reads_the_ledger_and_fails_open(tmp_path, monkeypatch) -> None:
    """`server.core.schema.migrate.pending_names`: repo files minus ledger rows; [] when the
    directory is empty or the service cannot be reached."""
    from server.core.schema import migrate as mig

    (tmp_path / "790_old.sql").write_text("SELECT 1")
    (tmp_path / "800_new.sql").write_text("SELECT 2")
    monkeypatch.setattr(mig, "client", lambda: object())
    monkeypatch.setattr(mig, "_applied", lambda ch: {"790_old.sql": "abc"})
    assert mig.pending_names(tmp_path) == ["800_new.sql"]
    # a directory that does not exist has nothing pending (no guess at the cwd)
    assert mig.pending_names(tmp_path / "missing") == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mig.pending_names(empty) == []

    def boom() -> object:
        raise ConnectionError("no service")

    monkeypatch.setattr(mig, "client", boom)
    assert mig.pending_names(tmp_path) == []
