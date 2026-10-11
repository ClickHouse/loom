"""The entity migration's statements against a recording client: each scope's values
read, a graph rename copying only what is not there yet (pairs that collapse dropped)
before deleting the old rows, degrees rebuilt where they disagree with the edges, and
memory rows re-emitted one version up."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.store import entity_migration as store


class _CH:
    def __init__(self, answers: dict[str, list[list[dict[str, Any]]]] | None = None) -> None:
        self.answers = answers or {}
        self.sql: list[tuple[str, dict[str, Any]]] = []

    def _answer(self, sql: str) -> list[dict[str, Any]]:
        for key, queue in self.answers.items():
            if key in sql and queue:
                return queue.pop(0)
        return []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append((sql, dict(params or {})))
        return self._answer(sql)

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append((sql, dict(params or {})))


@pytest.mark.asyncio
async def test_values_are_read_per_scope_from_every_entity_column() -> None:
    row = {"org": "o", "namespace": "n", "v": "api respons"}
    ch = _CH(
        {"FROM loom.entity_edges": [[row]], "FROM loom.memory_embeddings": [[{**row, "v": "kits"}]]}
    )
    assert await store.stored_values(ch=ch) == {("o", "n"): {"api respons", "kits"}}
    tables = {t for t, _cols, _pair in store.GRAPH_TABLES} | set(store.MEMORY_TABLES)
    assert all(any(f"FROM {t}" in sql for sql, _ in ch.sql) for t in tables)


@pytest.mark.asyncio
async def test_naming_texts_are_the_scopes_live_memories_that_name_a_value() -> None:
    ch = _CH({"arrayIntersect": [[{"v": "kits", "value": "the kits"}]]})
    assert await store.naming_texts(("o", "n"), ["kits"], ch=ch) == {"kits": ["the kits"]}
    sql, params = ch.sql[0]
    assert "tombstoned_at IS NULL" in sql and (params["org"], params["ns"]) == ("o", "n")


@pytest.mark.asyncio
async def test_counts_add_up_over_chunks() -> None:
    olds = [f"v{i}" for i in range(store._CHUNK + 1)]
    ch = _CH({"count()": [[{"n": 2}], [{"n": 3}], [{"n": 1}]]})
    assert (
        await store.count_graph(
            "loom.entity_edges", ("entity_a", "entity_b"), ("o", "n"), olds, ch=ch
        )
        == 5
    )
    assert await store.count_memory("loom.memory", ("o", "n"), ["v"], ch=ch) == 1


@pytest.mark.asyncio
async def test_a_rename_copies_what_is_not_there_then_deletes_the_old_rows() -> None:
    ch = _CH()
    await store.rename_graph(
        "loom.entity_edges", ("entity_a", "entity_b"), True, ("o", "n"), {"cas": "case"}, ch=ch
    )
    (insert, params), (delete, _) = ch.sql
    assert insert.startswith("INSERT INTO loom.entity_edges") and "EXCEPT ALL" in insert
    assert "least(" in insert and "WHERE entity_a != entity_b" in insert  # self-pairs dropped
    assert delete.startswith("DELETE FROM loom.entity_edges")
    assert (params["olds"], params["news"]) == (["cas"], ["case"])
    ch = _CH()
    await store.rename_graph(
        "loom.entity_vectors", ("entity_text",), False, ("o", "n"), {"a": "b"}, ch=ch
    )
    assert "least(" not in ch.sql[0][0] and "!=" not in ch.sql[0][0]


@pytest.mark.asyncio
async def test_degrees_are_rebuilt_where_they_disagree_with_the_edges() -> None:
    ch = _CH({"FULL OUTER JOIN": [[{"org": "o", "namespace": "n"}]]})
    assert await store.unsynced_degrees(ch=ch) == [("o", "n")]
    await store.rebuild_degrees(("o", "n"), ch=ch)
    delete, insert = (sql for sql, _ in ch.sql[1:])
    assert delete.startswith("DELETE FROM loom.entity_degree")
    assert insert.startswith("INSERT INTO loom.entity_degree") and "loom.entity_edges" in insert


@pytest.mark.asyncio
async def test_memory_rows_are_re_emitted_one_version_up(monkeypatch) -> None:
    key = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    ident = {**key, "memory_key": "k"}
    ch = _CH(
        {
            "SELECT org, namespace, agent": [[ident]],
            "SELECT * FROM loom.memory_embeddings": [
                [{**ident, "entities": ["kits"], "version": 7}]
            ],
        }
    )
    written: dict[str, list[dict[str, Any]]] = {}

    async def rows_by_keys(keys, *, ch=None):
        return {k: SimpleNamespace(entities=["kits"], version=5) for k in keys}

    async def insert_memory(rows, *, ch=None):
        written["memory"] = rows

    async def insert_embeddings(rows, *, ch=None):
        written["embeddings"] = rows

    monkeypatch.setattr(store, "memory_rows_by_identity_keys", rows_by_keys)
    monkeypatch.setattr(store, "build_memory_version_row", lambda m, **kw: kw)
    monkeypatch.setattr(store, "build_embedding_row", lambda **kw: kw)
    monkeypatch.setattr(store, "insert_memory_versions", insert_memory)
    monkeypatch.setattr(store, "insert_embedding_rows", insert_embeddings)
    out = await store.reemit_memory(
        ("o", "n"), ["kits"], lambda es: ["kit" if e == "kits" else e for e in es], ch=ch
    )
    assert out == {"loom.memory": 1, "loom.memory_embeddings": 1}
    assert written["memory"] == [{"entities": ["kit"], "version": 6}]
    assert (written["embeddings"][0]["entities"], written["embeddings"][0]["version"]) == (
        ["kit"],
        8,
    )
