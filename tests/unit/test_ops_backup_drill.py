"""scripts/ops/backup_drill.sh against a fake curl and fake backup and restore
tools: the password never reaches an argv, an incomplete manifest, a failed
table or a drifted table list fails the drill, and --full-restore restores
into the scratch database only, compares its counts with the manifest, maps
the restore's exit codes and drops the scratch database on every exit."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TABLES = "namespaces memory memory_embeddings documents chunks shared erasure_requests agent_events audit_log idempotency".split()

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$DRILL_ARGV"
cat >>"$DRILL_STDIN"
case "$*" in
  *version*) echo 26.8 ;;
  *'count() FROM `loom_drill`'*) echo "${DRILL_RESTORED:-2}" ;;
  *) echo 2 ;;
esac
"""

FAKE_BACKUP = """
import json, os, sys
out = sys.argv[sys.argv.index("--out-dir") + 1]
tables = os.environ["DRILL_TABLES"].split()
bad = os.environ.get("DRILL_FAIL", "")
for t in tables:
    open(os.path.join(out, t + ".jsonl"), "w").write("{}\\n{}\\n")
rows = [{"name": t, "rows": 2, **({"error": "boom"} if t == bad else {})} for t in tables]
complete = os.environ.get("DRILL_COMPLETE", "true") == "true"
json.dump({"complete": complete, "tables": rows}, open(os.path.join(out, "manifest.json"), "w"))
sys.exit(int(os.environ.get("DRILL_BACKUP_RC", "0")))
"""

FAKE_RESTORE = """
import os, sys
open(os.environ["DRILL_RESTORE_ARGV"], "w").write(" ".join(sys.argv[1:]))
sys.exit(int(os.environ.get("DRILL_RESTORE_RC", "0")))
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    if not shutil.which("jq"):
        pytest.skip("jq not installed")
    (tmp_path / "scripts/ops").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/ops/backup_drill.sh", tmp_path / "scripts/ops/")
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev/backup_loom.py").write_text(FAKE_BACKUP)
    (tmp_path / "dev/restore_loom.py").write_text(FAKE_RESTORE)
    (tmp_path / ".env").write_text(
        "LOOM_CH_HOST=ch.example\nLOOM_CH_PORT=8443\nLOOM_CH_USER=u\n"
        "LOOM_CH_PASSWORD='s3cr\"et'\nLOOM_CH_DATABASE=loom\n"
    )
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/curl").write_text(FAKE_CURL)
    (tmp_path / "bin/curl").chmod(0o755)
    return tmp_path


def _drill(box: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {
        **os.environ,
        "PATH": f"{box / 'bin'}:{os.environ['PATH']}",
        "PYTHON": sys.executable,
        "DRILL_ARGV": str(box / "argv"),
        "DRILL_STDIN": str(box / "stdin"),
        "DRILL_RESTORE_ARGV": str(box / "restore_argv"),
        "DRILL_TABLES": " ".join(TABLES),
        **env,
    }
    return subprocess.run(
        ["bash", str(box / "scripts/ops/backup_drill.sh"), *args],
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_clean_dump_passes_and_the_password_stays_out_of_argv(sandbox: Path) -> None:
    r = _drill(sandbox)
    assert r.returncode == 0, r.stderr
    assert "all 10 table files match row counts" in r.stdout
    argv = (sandbox / "argv").read_text()
    assert "s3cr" not in argv and "https://ch.example:8443/" in argv
    assert 'user = "u:s3cr\\"et"' in (sandbox / "stdin").read_text()


def test_a_table_the_backup_failed_fails_the_drill(sandbox: Path) -> None:
    r = _drill(sandbox, DRILL_FAIL="audit_log")
    assert r.returncode == 1 and "recorded errors for: audit_log" in r.stderr


def test_a_table_list_that_drifted_from_the_dump_fails_the_drill(sandbox: Path) -> None:
    r = _drill(sandbox, DRILL_TABLES=" ".join(TABLES[:-1]))
    assert r.returncode == 1 and "manifest lists" in r.stderr


def test_an_incomplete_manifest_fails_the_drill(sandbox: Path) -> None:
    r = _drill(sandbox, DRILL_COMPLETE="false")
    assert r.returncode == 1 and "not marked complete" in r.stderr


def test_a_partial_backup_is_named(sandbox: Path) -> None:
    r = _drill(sandbox, DRILL_BACKUP_RC="1")
    assert r.returncode == 1 and "the backup is partial" in r.stderr


def _statements(box: Path) -> list[str]:
    return (box / "argv").read_text().splitlines()


def test_a_full_restore_loads_the_scratch_database_and_drops_it(sandbox: Path) -> None:
    r = _drill(sandbox, "--full-restore")
    assert r.returncode == 0, r.stderr
    assert "every restored table matches the manifest" in r.stdout
    argv = (sandbox / "restore_argv").read_text()
    assert argv.endswith("--database loom_drill") and "--password" not in argv
    statements = _statements(sandbox)
    create = next(i for i, s in enumerate(statements) if "CREATE DATABASE `loom_drill`" in s)
    assert "DROP DATABASE IF EXISTS `loom_drill`" in statements[create - 1]
    assert "DROP DATABASE IF EXISTS `loom_drill`" in statements[-1]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"DRILL_RESTORE_RC": "2"}, "the restore refused"),
        ({"DRILL_RESTORE_RC": "1"}, "the restore is partial"),
        ({"DRILL_RESTORED": "1"}, "10 tables restored a row count other than the manifest's"),
    ],
)
def test_a_failed_restore_is_named_and_the_scratch_database_dropped(
    sandbox: Path, env: dict[str, str], message: str
) -> None:
    r = _drill(sandbox, "--full-restore", **env)
    assert r.returncode == 1 and message in r.stderr
    assert "DROP DATABASE IF EXISTS `loom_drill`" in _statements(sandbox)[-1]


def test_an_unknown_argument_changes_nothing(sandbox: Path) -> None:
    r = _drill(sandbox, "--bogus")
    assert r.returncode == 1 and not (sandbox / "argv").exists()
