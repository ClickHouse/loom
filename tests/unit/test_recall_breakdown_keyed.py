"""The inspector's served breakdown reads by the table's key.

`loom.memory_usage_events` is sorted (org, namespace, memory_id, ts); the
per-agent breakdown `memory.get` computes for the drawer would scan every row
the table holds if it filtered on memory_id alone. The statement names the org
and namespace the key belongs to, and the ids of the key's live rows, so the key prunes
it; the id lookup is itself bounded to that (org, namespace).
"""

from __future__ import annotations

from server.memory.store import usage


def test_the_breakdown_statement_filters_by_the_sort_key() -> None:
    sql = usage._SERVED_BY_AGENT_SQL
    outer = sql[: sql.index("memory_id IN (")]
    assert "FROM loom.memory_usage_events" in outer
    assert outer.index("org = {org:String}") < outer.index("namespace = {ns:String}")
    inner = sql[sql.index("memory_id IN (") : sql.index("event_type IN")]
    assert "org = {org:String} AND namespace = {ns:String}" in inner
