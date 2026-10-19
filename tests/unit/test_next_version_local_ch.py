"""`next_version`'s SQL on `clickhouse local`: the version passes the newest row
of its key, however far ahead the clock that wrote it, and is ClickHouse's
clock otherwise. The fakes elsewhere stand in for this read; here it runs."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from server.core.versions import next_version

ROOT = Path(__file__).resolve().parents[2]
CLICKHOUSE = shutil.which("clickhouse")
pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _ddl(table: str) -> str:
    schema = (ROOT / "schemas" / "schema.sql").read_text()
    body = schema.split(f"CREATE TABLE IF NOT EXISTS loom.{table}\n", 1)[1]
    return f"CREATE TABLE loom.{table}\n" + body.split(";", 1)[0]


class _LocalCH:
    def __init__(self, rows: str) -> None:
        self.rows = rows

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._run(sql, params)

    def _run(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        for name, value in params.items():
            sql = sql.replace(f"{{{name}:String}}", f"'{value}'")
        script = ";\n".join(
            ["CREATE DATABASE loom", _ddl("sso_links"), self.rows, sql + " FORMAT JSONEachRow"]
        )
        out = subprocess.run(
            [CLICKHOUSE or "clickhouse", "local", "--multiquery", "-q", script],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        return lines[0] if lines else None


_AHEAD = 2**62  # written by a host whose clock runs far ahead


def test_the_next_version_passes_the_newest_row_of_its_key() -> None:
    ch = _LocalCH(
        "INSERT INTO loom.sso_links (email, org, namespace, linked, linked_by, linked_at, version) "
        f"VALUES ('a@x.io', 'o', 'n', 1, 'op', now64(3), {_AHEAD})"
    )
    assert int(asyncio.run(next_version(ch, "loom.sso_links", "email", "a@x.io"))) == _AHEAD + 1


def test_a_key_with_no_row_gets_clickhouses_clock() -> None:
    ch = _LocalCH(
        "INSERT INTO loom.sso_links (email, org, namespace, linked, linked_by, linked_at, version) "
        f"VALUES ('a@x.io', 'o', 'n', 1, 'op', now64(3), {_AHEAD})"
    )
    before = time.time_ns()
    got = int(asyncio.run(next_version(ch, "loom.sso_links", "email", "b@x.io")))
    assert before - 60 * 10**9 < got < time.time_ns() + 60 * 10**9  # now, not the other key's
