"""The inspector's served breakdown reads by the table's key.

`loom.memory_usage_events` is sorted (org, namespace, memory_id, ts); the
per-agent breakdown `memory.get` computes for the drawer would scan every row
the table holds if it filtered on memory_id alone. The statement names the org
and namespace the key belongs to, and the ids of the user's live rows of the key, so the
key prunes it; the id lookup is itself bounded to that (org, namespace).
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.memory.store import usage


def test_the_breakdown_statement_filters_by_the_sort_key() -> None:
    sql = usage._SERVED_BY_AGENT_SQL
    outer = sql[: sql.index("memory_id IN (")]
    assert "FROM loom.memory_usage_events" in outer
    assert outer.index("org = {org:String}") < outer.index("namespace = {ns:String}")
    inner = sql[sql.index("memory_id IN (") : sql.index("event_type IN")]
    assert "org = {org:String} AND namespace = {ns:String}" in inner
    assert "memory_key = {key:String}" in inner and "tombstoned_at IS NULL" in inner
    # another user's copy of the key is not this user's to count
    assert "user_id = {uid:String}" in inner


def test_the_breakdown_binds_the_rows_identity_and_both_event_kinds() -> None:
    seen: dict[str, Any] = {}

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            seen.update(params or {})
            return []

    asyncio.run(usage.served_by_agent("o", "n", "u", "k", ch=_CH()))  # type: ignore[arg-type]
    assert seen == {"org": "o", "ns": "n", "uid": "u", "key": "k", "types": ["recall", "served"]}
