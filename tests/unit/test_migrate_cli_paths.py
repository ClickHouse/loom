"""`loom migrate` finds this checkout's journal from any working directory."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from server.cli import serve
from server.core.paths import MIGRATIONS_DIR
from server.core.schema import migrate


class _Result:
    result_rows: list[tuple] = []


class _CH:
    def query(self, *a, **k):
        return _Result()


def test_migrate_resolves_the_journal_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(migrate, "client", lambda: _CH())
    assert migrate.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert f"total: {len(list(MIGRATIONS_DIR.glob('*.sql')))}" in out
    assert "migrations directory not found" not in out


def test_loom_migrate_without_flags_leaves_the_runner_its_default() -> None:
    ns = argparse.Namespace(
        status=False, dry_run=False, strict=False, target=None, migrations_dir=None
    )
    assert serve._migrate_argv(ns) == []
