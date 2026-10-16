"""loom.sso_links rows carry every column: a link and its undo are both whole
rows, so FINAL never serves a column's DEFAULT over what a link wrote."""

from __future__ import annotations

from typing import Any

import pytest

from server.auth import sso_links
from tests.unit.schema_ddl import column_names

pytestmark = pytest.mark.asyncio


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert table == "loom.sso_links"
        self.rows.extend(rows)


async def test_a_link_and_an_unlink_write_every_column(monkeypatch) -> None:
    ch = _Recorder()
    monkeypatch.setattr(sso_links, "get_client", lambda: ch)
    await sso_links.link("Lee@Example.com", [("acme", "lee")], "self")
    await sso_links.unlink("lee@example.com", [("acme", "lee")], "operator:root")
    assert [sorted(r) for r in ch.rows] == [sorted(column_names("sso_links"))] * 2
    assert [(r["email"], r["linked"]) for r in ch.rows] == [
        ("lee@example.com", 1),
        ("lee@example.com", 0),
    ]
