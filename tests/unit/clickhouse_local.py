"""Run a statement the server issues on clickhouse-local against the production table
definitions in schemas/schema.sql, for the unit tests that check real SQL. Tests that use
it skip when the `clickhouse` binary is not installed (`CLICKHOUSE` is None)."""

from __future__ import annotations

import json
import re
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


def render(sql: str, params: dict[str, Any]) -> str:
    """`sql` with each `{name:Type}` parameter written in as a literal."""
    return re.sub(r"\{(\w+):[^}]+\}", lambda m: _literal(params[m.group(1)]), sql)


def run(setup: list[str], sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The rows `sql` returns after the `setup` statements, in a fresh `loom` database."""
    script = ";\n".join(["CREATE DATABASE loom", *setup, render(sql, params or {})])
    out = subprocess.run(
        [CLICKHOUSE or "clickhouse", "local", "--multiquery", "-q", script + " FORMAT JSONEachRow"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]
