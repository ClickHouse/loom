"""memory.timeline serves live rows only: the latest version of a deleted row
keeps its vector (embedding_dim > 0), so the read must say tombstoned_at IS NULL,
and expired or retired rows stay out as they do on every other read."""

from __future__ import annotations

import asyncio
from typing import Any

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app.curation import fetch_timeline


class _CH:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.sql.append(sql)
        return []


def test_timeline_reads_only_live_served_rows() -> None:
    ch = _CH()
    asyncio.run(fetch_timeline(org="o", namespace="n", entity="nate", ch=ch, scope=WHOLE_NAMESPACE))  # type: ignore[arg-type]
    [sql] = ch.sql
    assert "tombstoned_at IS NULL" in sql
    assert "(expires_at IS NULL OR expires_at > now())" in sql
    assert "status IN ('active', 'conflicted')" in sql
