"""A user's facts leave the entity graph when only that user's
memories carried them, and a user's link rows are matched by (namespace,
memory_key) — never by the generic key alone."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from server.erasure.store import predicates as erasure_predicates
from server.erasure.store import steps_graph as graph_steps

# cityHash64 of each owned key, as ClickHouse returns it
_OWNED = {
    "loom.entity_triple_memories": [{"h": 11}],
    "loom.entity_edge_memories": [{"h": 22}, {"h": 21}],
}


class _CH:
    """Counts come from `count`; a read of a link table gets the keys `_OWNED` lists."""

    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), dict(params or {})))
        return {"n": self.count}

    async def execute(self, sql, params=None):
        self.commands.append((" ".join(sql.split()), dict(params or {})))

    async def query_all(self, sql, params=None):
        if "system.mutations" not in sql:
            self.reads.append((" ".join(sql.split()), dict(params or {})))
            return next((rows for t, rows in _OWNED.items() if f"FROM {t} " in sql), [])
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]


def test_user_link_rows_are_matched_by_namespace_and_key() -> None:
    assert erasure_predicates.USER_PARENT_KEYS.startswith(
        "(namespace, memory_key) IN (SELECT namespace, memory_key FROM loom.memory"
    )
    assert "user_id = {uid:String}" in erasure_predicates.USER_PARENT_KEYS


async def test_only_triples_and_edges_owned_by_the_user_alone_are_deleted() -> None:
    ch = _CH(count=2)
    n = await graph_steps.delete_entity_graph_by_user("o", "alice", ch=ch)
    assert n == 4
    tables = [c[0].split(" ")[2] for c in ch.commands]
    assert tables == ["loom.entity_triples", "loom.entity_edges"]
    # ownership is read from the link tables, then the keys found are bound by hash
    (tread, tread_params), (eread, _) = ch.reads
    assert tread.startswith(
        "SELECT cityHash64(namespace, subject, relation, object) AS h "
        "FROM loom.entity_triple_memories FINAL"
    )
    # the user's keys and those of the summaries built from them
    keys = erasure_predicates.USER_GRAPH_KEYS
    assert f"HAVING countIf({keys}) > 0 AND countIf(NOT {keys}) = 0" in tread
    assert tread_params == {"org": "o", "uid": "alice"}
    assert eread.startswith(
        "SELECT cityHash64(namespace, entity_a, entity_b) AS h FROM loom.entity_edge_memories"
    )
    tsql, tparams = ch.commands[0]
    assert "cityHash64(namespace, subject, relation, object) IN {owned:Array(UInt64)}" in tsql
    assert tparams["owned"] == [11]
    assert "SELECT" not in tsql and "mutations_sync = 0" in tsql
    esql, eparams = ch.commands[1]
    assert "cityHash64(namespace, entity_a, entity_b) IN {owned:Array(UInt64)}" in esql
    assert eparams["owned"] == [21, 22]
    # the count reads the same predicate the mutation uses
    assert ch.queries[0][0].split("WHERE", 1)[1].strip() in tsql


async def test_the_user_saga_deletes_the_graph_before_it_deletes_the_links(monkeypatch) -> None:
    import server.erasure.app.saga as h

    calls: list[str] = []

    async def fake_do_step(*, step_name, step_fn, completed, row, **kw):
        calls.append(step_name)
        return SimpleNamespace(step=step_name, rows_affected=0, status="completed")

    monkeypatch.setattr(h, "_do_step", fake_do_step)
    await h._saga_user_id("o", "alice", completed=set(), row={})
    assert "entity_graph.delete" in calls
    assert calls.index("entity_graph.delete") < calls.index("graph_links.delete")
    assert calls.index("memory.tombstone") < calls.index(
        "entity_graph.delete"
    )  # keys still resolve: tombstone, not delete
