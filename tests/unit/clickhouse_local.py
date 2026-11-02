"""Run a statement the server issues on clickhouse-local against the production table
definitions in schemas/schema.sql, for the unit tests that check real SQL. Tests that use
it skip when the `clickhouse` binary is not installed (`CLICKHOUSE` is None). The session
reads and writes `DateTime` in UTC, as the server pins it (`server/core/ch.py`), whatever
the host's zone."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clickhouse_connect.driver.binding import bind_query

ROOT = Path(__file__).resolve().parents[2]
CLICKHOUSE = shutil.which("clickhouse")


def ddl(table: str) -> str:
    """The `CREATE TABLE` for `loom.<table>` as schema.sql has it."""
    schema = (ROOT / "schemas" / "schema.sql").read_text()
    body = schema.split(f"CREATE TABLE IF NOT EXISTS loom.{table}\n", 1)[1]
    return f"CREATE TABLE loom.{table}\n" + body.split(";", 1)[0]


def view(name: str) -> str:
    """The `CREATE MATERIALIZED VIEW` for `loom.<name>` as schema.sql has it."""
    schema = (ROOT / "schemas" / "schema.sql").read_text()
    head = f"CREATE MATERIALIZED VIEW IF NOT EXISTS loom.{name} "
    return head + schema.split(head, 1)[1].split(";", 1)[0]


def run(
    setup: list[str],
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """The rows `sql` returns after the `setup` statements, in a fresh `loom` database, or
    in the one kept under `path` so a later call sees what this one wrote. Its
    `{name:Type}` parameters are bound by type, as the server binds them, so a parameter
    whose type does not fit its column fails or matches what it would in production."""
    return _output(setup, sql, params, path=path, fmt="JSONEachRow")


def _output(
    setup: list[str], sql: str, params: dict[str, Any] | None, *, path: Path | None, fmt: str
) -> list[Any]:
    """The parsed lines `sql` prints in `fmt` after the `setup` statements (see `run`)."""
    script = ";\n".join(["CREATE DATABASE IF NOT EXISTS loom", *setup, sql])
    # The server's own binder, which also keeps a DateTime64 value's fraction; the
    # session is in UTC, as the server's is, since a zone-less one is read in it.
    _, bound = bind_query(script, params or {}, UTC)
    binds = [f"--{k}={v}" for k, v in bound.items()]
    kept = ["--path", str(path)] if path else []
    # The script goes on stdin: a test that keeps vector rows outgrows one argument's limit.
    out = subprocess.run(
        [
            CLICKHOUSE or "clickhouse",
            "local",
            "--multiquery",
            "--session_timezone=UTC",
            *kept,
            *binds,
        ],
        input=f"{script} FORMAT {fmt}",
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise AssertionError(out.stderr.strip()[-2000:])
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


# one embedding at the codec's declared width, and the setup its table needs
VECTOR = "[" + ",".join(["0.1"] * 3072) + "]"
EMBEDDINGS = ("SET enable_quantized_codec = 1", ddl("memory_embeddings"))


class LocalCH:
    """A ClickHouse client over clickhouse-local that runs the `setup` statements before
    each read, so a test's rows are the whole database."""

    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(self.setup, sql, params)


_CREATED = re.compile(r"\bCREATE TABLE (?:IF NOT EXISTS )?([\w.]+)")
# Side tables a write path inserts into and no KeptCH test reads back.
_UNREAD = {"loom.audit_log", "loom.memory_projection_failures"}


class KeptCH:
    """A ClickHouse client over clickhouse-local whose database is kept under `path`, so
    each statement sees what the earlier ones wrote. An insert into one of the `_UNREAD`
    side tables is dropped unless the fixture created it; any other statement on a table
    the fixture did not create raises. Statement `settings` are ignored, and a row is
    inlined between `$$` quotes, so a value holding `$$` cannot be inserted."""

    def __init__(self, path: Path, *setup: str) -> None:
        self.path = path
        self.tables: set[str] = set()
        self.create(*setup)

    def create(self, *statements: str) -> None:
        """Run DDL (with any session `SET` it needs) in one session."""
        run(list(statements), "SELECT 1", path=self.path)
        self.tables |= {m.group(1) for s in statements for m in _CREATED.finditer(s)}

    async def execute(
        self, sql: str, params: dict[str, Any] | None = None, *, settings: Any = None
    ) -> None:
        run([sql], "SELECT 1", params, path=self.path)

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if table in _UNREAD and table not in self.tables:
            return
        for r in rows:
            cols = ", ".join(r)
            data = json.dumps(r, default=str)
            await self.execute(
                f"INSERT INTO {table} ({cols}) SELECT {cols} FROM format(JSONEachRow, $${data}$$)"
            )

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        lines = _output([], sql, params, path=self.path, fmt="JSONCompactEachRowWithNamesAndTypes")
        names, types, *rows = lines
        # A DateTime column as the driver answers it: a datetime, not JSON text.
        dated = [t.removeprefix("Nullable(").startswith("DateTime") for t in types]
        return [
            {
                k: datetime.fromisoformat(v) if d and isinstance(v, str) else v
                for k, d, v in zip(names, dated, row, strict=True)
            }
            for row in rows
        ]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None
