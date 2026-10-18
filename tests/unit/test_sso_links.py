"""loom.sso_links rows carry every column: a link and its undo are both whole
rows, so FINAL never serves a column's DEFAULT over what a link wrote."""

from __future__ import annotations

import time
from typing import Any

import pytest

from server.auth import sso_links
from tests.unit.schema_ddl import column_names

pytestmark = pytest.mark.asyncio


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        # `next_version`'s read, past a row a replica ahead wrote: the next one
        # must still be newer.
        newest = max((r["version"] for r in self.rows), default=2**62)
        return {"v": max(time.time_ns(), newest + 1)}

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert table == "loom.sso_links"
        self.rows.extend(rows)


async def test_a_link_and_an_unlink_write_every_column(monkeypatch) -> None:
    ch = _Recorder()
    monkeypatch.setattr(sso_links, "get_client", lambda: ch)
    await sso_links.link("Lee@Example.com", [("acme", "lee")], "self")
    await sso_links.unlink("lee@example.com", [("acme", "lee")], "operator:root")
    assert [sorted(r) for r in ch.rows] == [sorted(column_names("sso_links"))] * 2
    assert ch.rows[0]["version"] > 2**62 and ch.rows[1]["version"] > ch.rows[0]["version"]
    assert [(r["email"], r["linked"]) for r in ch.rows] == [
        ("lee@example.com", 1),
        ("lee@example.com", 0),
    ]
