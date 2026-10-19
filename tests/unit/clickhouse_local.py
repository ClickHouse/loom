"""Run a statement the server issues on clickhouse-local against the production table
definitions in schemas/schema.sql, for the unit tests that check real SQL. Tests that use
it skip when the `clickhouse` binary is not installed (`CLICKHOUSE` is None)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CLICKHOUSE = shutil.which("clickhouse")


def ddl(table: str) -> str:
    """The `CREATE TABLE` for `loom.<table>` as schema.sql has it."""
    schema = (ROOT / "schemas" / "schema.sql").read_text()
    body = schema.split(f"CREATE TABLE IF NOT EXISTS loom.{table}\n", 1)[1]
    return f"CREATE TABLE loom.{table}\n" + body.split(";", 1)[0]


def _literal(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, list):
        return "[" + ",".join(_literal(x) for x in v) + "]"
    if isinstance(v, tuple):
        return "(" + ",".join(_literal(x) for x in v) + ")"
    text = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _param(v: Any) -> str:
    """`v` as a `--param_<name>` value: a scalar goes in bare (escaped as TSV), a container
    as a literal, so the server parses it with the statement's declared type."""
    if v is None:
        return "\\N"
    if isinstance(v, list | tuple | bool):
        return _literal(v)
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def run(setup: list[str], sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The rows `sql` returns after the `setup` statements, in a fresh `loom` database. Its
    `{name:Type}` parameters are bound by type, as the server binds them, so a parameter
    whose type does not fit its column fails or matches what it would in production."""
    script = ";\n".join(["CREATE DATABASE loom", *setup, sql])
    binds = [f"--param_{k}={_param(v)}" for k, v in (params or {}).items()]
    out = subprocess.run(
        [
            CLICKHOUSE or "clickhouse",
            "local",
            "--multiquery",
            *binds,
            "-q",
            script + " FORMAT JSONEachRow",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise AssertionError(out.stderr.strip()[-2000:])
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
