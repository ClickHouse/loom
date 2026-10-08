"""The inspector's recall breakdown reads by the
table's key.

`loom.memory_usage_events` is sorted (org, namespace, memory_id, ts); the
per-agent recall breakdown `memory.get` computes for the drawer would scan
every row the table holds if it filtered on memory_id alone. The statement
names the org and namespace the row belongs to, so the key prunes it.
"""

from __future__ import annotations

import re

from server.memory.app import handlers as memory_handler


def test_the_breakdown_statement_filters_by_the_sort_key() -> None:
    src = open(memory_handler.__file__, encoding="utf-8").read()
    start = src.index("FROM loom.memory_usage_events")
    stmt = src[src.rindex("SELECT", 0, start) : src.index("GROUP BY agent", start)]
    where = stmt[stmt.index("WHERE") :]
    assert re.search(r"org = \{org:String\}", where), where
    assert re.search(r"namespace = \{ns:String\}", where), where
    assert "memory_id = {mid:String}" in where
    assert where.index("org = {org:String}") < where.index("memory_id = {mid:String}")
    params = src[src.index("GROUP BY agent", start) : src.index("recall_by_agent = [", start)]
    assert '"org": row.org' in params and '"ns": row.namespace' in params
