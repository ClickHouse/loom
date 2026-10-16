"""The migration ledger against a migrated store: `--status --strict` is the
deploy preflight, so it must pass on the repo's own chain and fail on an
edited applied file or a renamed and edited one; a file renamed with its text
unchanged is recognised by its checksum. Read-only: every case points the runner
at a copied migrations directory and never applies anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server.core.schema.journal import baseline_covers

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "schemas" / "migrations"

pytestmark = pytest.mark.usefixtures("ch_target_required")


def _status(mig_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "server.core.schema.migrate", "--status", "--strict"]
        + ["--migrations-dir", str(mig_dir)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def chain(tmp_path: Path) -> Path:
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, copy)
    return copy


def test_the_repo_chain_matches_the_ledger(chain: Path) -> None:
    r = _status(chain)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "checksum drift: 0" in r.stdout and "pending: 0" in r.stdout, r.stdout


def test_an_edited_applied_migration_is_drift(chain: Path) -> None:
    edited = sorted(chain.glob("*.sql"))[-1]
    edited.write_text(edited.read_text() + "\n-- edited after it was applied\n")
    r = _status(chain)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"checksum drift: {edited.name}" in r.stdout, r.stdout


def _newest_plain(chain: Path) -> Path:
    # Not a baseline: a renamed baseline would supersede the row it left behind.
    return [f for f in sorted(chain.glob("*.sql")) if baseline_covers(f) is None][-1]


def test_a_renamed_applied_migration_is_recognised_by_its_checksum(chain: Path) -> None:
    """The same text renumbered (a branch migration moved past main's) already ran:
    reported as renamed, and the preflight passes."""
    old = _newest_plain(chain)
    new = old.with_name("999_" + old.name.split("_", 1)[1])
    old.rename(new)
    r = _status(chain)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ledger-only: 0" in r.stdout, r.stdout
    assert f"{old.name} is {new.name} under an earlier name" in r.stdout, r.stdout


def test_a_renamed_and_edited_applied_migration_is_ledger_only(chain: Path) -> None:
    old = _newest_plain(chain)
    old.with_name("999_renamed.sql").write_text(old.read_text() + "\n-- edited\n")
    old.unlink()
    r = _status(chain)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ledger-only: 1" in r.stdout and "pending: 1" in r.stdout, r.stdout
    assert f"ledger-only: {old.name}" in r.stdout, r.stdout
