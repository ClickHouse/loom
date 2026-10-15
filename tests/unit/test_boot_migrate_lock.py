"""The boot-migration step behind LOOM_BOOT_MIGRATE. The runner it calls takes
the deployment's migration lock itself (test_migrate_lease.py, test_migrate_keeper_lock.py)."""

from __future__ import annotations

from server.boot import run_boot_migrations_if_enabled


def test_boot_migrate_switch_skips_the_step_when_off(caplog) -> None:
    """LOOM_BOOT_MIGRATE=false applies nothing and returns 0."""
    calls: list[list[str]] = []

    def migrate(argv: list[str]) -> int:
        calls.append(argv)
        return 7

    assert run_boot_migrations_if_enabled(migrate, enabled=True) == 7
    assert calls == [[]]
    with caplog.at_level("WARNING"):
        assert run_boot_migrations_if_enabled(migrate, enabled=False) == 0
    assert calls == [[]]  # not called again
    assert any("boot migrations skipped" in r.message for r in caplog.records)


def test_boot_migrate_defaults_on() -> None:
    from server.settings import Settings

    assert Settings.model_fields["boot_migrate"].default is True


def test_boot_migrate_skip_names_the_pending_files(caplog) -> None:
    """The skip names what the service lacks, in the
    WARNING and in the module's snapshot for /v1/health."""
    import server.boot as boot_mod

    with caplog.at_level("WARNING"):
        rc = run_boot_migrations_if_enabled(
            lambda argv: 7,
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
        assert run_boot_migrations_if_enabled(lambda argv: 7, enabled=False, pending=list) == 0
    assert boot_mod.SKIPPED_PENDING_MIGRATIONS == ()
    assert any("nothing is pending" in r.getMessage() for r in caplog.records)
    # Enabled: the snapshot is reset — the deployment applied everything.
    assert run_boot_migrations_if_enabled(lambda argv: 3, enabled=True, pending=lambda: ["x"]) == 3
    assert boot_mod.SKIPPED_PENDING_MIGRATIONS == ()


def test_boot_migrate_skip_pending_probe_fails_open(caplog) -> None:
    import server.boot as boot_mod

    def boom() -> list[str]:
        raise RuntimeError("ledger unreachable")

    with caplog.at_level("WARNING"):
        assert run_boot_migrations_if_enabled(lambda argv: 7, enabled=False, pending=boom) == 0
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
    monkeypatch.setattr(mig, "applied_migrations", lambda ch: {"790_old.sql": "abc"})
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
