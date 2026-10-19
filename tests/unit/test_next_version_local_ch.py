"""`next_version`'s SQL on `clickhouse local`: the version passes the newest row
of its key, however far ahead the clock that wrote it, and is ClickHouse's
clock otherwise. The fakes elsewhere stand in for this read; here it runs."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.core.versions import next_version
from tests.unit import clickhouse_local

pytestmark = pytest.mark.skipif(
    clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed"
)


class _LocalCH:
    def __init__(self, rows: str) -> None:
        self.rows = rows

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        setup = [clickhouse_local.ddl("sso_links"), self.rows]
        rows = clickhouse_local.run(setup, sql, params)
        return rows[0] if rows else None


_AHEAD = 2**62  # written by a host whose clock runs far ahead


def test_the_next_version_passes_the_newest_row_of_its_key() -> None:
    ch = _LocalCH(
        "INSERT INTO loom.sso_links (email, org, namespace, linked, linked_by, linked_at, version) "
        f"VALUES ('a@x.io', 'o', 'n', 1, 'op', now64(3), {_AHEAD})"
    )
    assert int(asyncio.run(next_version(ch, "loom.sso_links", {"email": "a@x.io"}))) == _AHEAD + 1


def test_a_key_with_no_row_gets_clickhouses_clock() -> None:
    ch = _LocalCH(
        "INSERT INTO loom.sso_links (email, org, namespace, linked, linked_by, linked_at, version) "
        f"VALUES ('a@x.io', 'o', 'n', 1, 'op', now64(3), {_AHEAD})"
    )
    before = time.time_ns()
    got = int(asyncio.run(next_version(ch, "loom.sso_links", {"email": "b@x.io"})))
    assert before - 60 * 10**9 < got < time.time_ns() + 60 * 10**9  # now, not the other key's
