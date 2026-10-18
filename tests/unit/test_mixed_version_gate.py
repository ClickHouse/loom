"""The mixed-version gate skips cleanly where it cannot run."""

from __future__ import annotations

import pytest

from scripts.gates import mixed_version


def test_without_docker_the_gate_skips_with_exit_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mixed_version.shutil, "which", lambda _name: None)
    assert mixed_version.main([]) == 0
    assert "skipped" in capsys.readouterr().out


def test_with_require_the_gate_fails_where_it_cannot_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mixed_version.shutil, "which", lambda _name: None)
    assert mixed_version.main(["--require"]) == 1
    assert "cannot run" in capsys.readouterr().out


def test_without_npm_no_bundle_is_built(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    monkeypatch.setattr(mixed_version.shutil, "which", lambda _name: None)
    assert mixed_version.build_frontend(tmp_path) is False


def test_the_next_tree_carries_one_migration_more(tmp_path) -> None:
    migrations = tmp_path / "schemas" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "029_instances.sql").write_text("SELECT 1;\n")
    (migrations / "030_shared_replica_state.sql").write_text("SELECT 1;\n")
    assert mixed_version.add_noop_migration(tmp_path) == "031_mixed_version_gate.sql"
    assert (migrations / "031_mixed_version_gate.sql").read_text().endswith("SELECT 1;\n")
