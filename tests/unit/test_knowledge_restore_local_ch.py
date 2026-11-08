"""The purge undo's DELETE on `clickhouse local`: it removes the one row the restore
wrote, by its version, and leaves the delete's row of the same source."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.knowledge.store import restore as restore_store
from tests.unit import clickhouse_local

pytestmark = pytest.mark.skipif(
    clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed"
)


def test_the_purge_undo_deletes_only_the_row_the_restore_wrote(tmp_path: Path) -> None:
    # One part, not folded on insert: no merge can fold the two rows before the DELETE.
    rows = (
        "INSERT INTO loom.documents (org, namespace, source_uri, version, superseded_at)"
        " SETTINGS optimize_on_insert = 0 VALUES"
        " ('acme', 'team', 'doc://a', 7, now()), ('acme', 'team', 'doc://a', 9, NULL)"
    )

    async def scenario() -> list[dict[str, Any]]:
        ch = clickhouse_local.KeptCH(tmp_path, clickhouse_local.ddl("documents"))
        await ch.execute(rows)
        await restore_store.delete_row("acme", "team", "doc://a", 9, ch=ch)  # type: ignore[arg-type]
        return await ch.query_all("SELECT version FROM loom.documents")

    assert [r["version"] for r in asyncio.run(scenario())] == [7]
