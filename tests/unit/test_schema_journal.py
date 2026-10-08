"""The journal module is the one reader of migration files, and it stands
alone: the health view and the rebuild tooling read the journal without
importing the runner."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from server.core.schema.journal import created_objects, object_ddl

ROOT = Path(__file__).resolve().parents[2]


def test_object_ddl_reads_creates_and_drops_in_order() -> None:
    sql = (
        "-- CREATE TABLE loom.commented (a Int8);\n"
        "CREATE TABLE IF NOT EXISTS loom.a (x Int8) ENGINE = Memory;\n"
        "CREATE MATERIALIZED VIEW IF NOT EXISTS loom.a_mv TO loom.a AS SELECT 1 AS x;\n"
        "create or replace view `loom`.`b` AS SELECT 1;\n"
        "DROP VIEW IF EXISTS loom.a_mv;\n"
        "ALTER TABLE loom.a DROP INDEX IF EXISTS i;\n"
    )
    assert list(object_ddl(sql)) == [
        ("create", "a"),
        ("create", "a_mv"),
        ("create", "b"),
        ("drop", "a_mv"),
    ]
    assert created_objects(sql) == ("a", "a_mv", "b")


def test_status_and_table_rebuild_do_not_import_the_runner() -> None:
    probe = (
        "import sys\n"
        "import server.core.schema.status, server.core.schema.table_rebuild\n"
        "print('server.core.schema.migrate' in sys.modules)\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "LOOM_ENV_FILE": "", "PYTHONPATH": str(ROOT)}
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "False"
