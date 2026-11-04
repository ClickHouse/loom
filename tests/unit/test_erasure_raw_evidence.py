"""Erasure reaches the raw transcripts, not only the memories made from them.

Every memory.set_from_messages call stores the caller's messages verbatim in
loom.memory_evidence; a deferred call parks the whole request in
loom.memory_extraction_queue; loom.memory_chunks holds the memory text
verbatim. The erasure saga covers all three, so a user erasure does not
tombstone the memory and leave its source sentences readable. CH-free.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.erasure.store import census as erasure_census
from server.erasure.store import predicates as erasure_predicates
from server.erasure.store import steps_memory as memory_steps


class _StubCH:
    def __init__(self, counts: list[int]) -> None:
        self.counts = counts
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.read_calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return {"n": self.counts.pop(0)} if self.counts else {"n": 0}

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))

    async def query_all(self, sql, params=None):
        if "system.mutations" not in sql:  # the user's memory keys
            self.read_calls.append((sql, params or {}))
            return [{"namespace": "ns-a", "memory_key": "user_employer"}]
        return [
            {
                "mutation_id": "mutation_1.txt",
                "is_done": 1,
                "parts_to_do": 0,
                "latest_fail_reason": "",
            }
        ]


def test_user_scope_deletes_evidence_and_queue_by_user_id() -> None:
    ch = _StubCH([3, 1])
    n = asyncio.run(memory_steps.delete_raw_evidence_by_user("o", "alice", ch=ch))
    assert n == 4
    tables = [sql.split("ALTER TABLE")[1].split()[0] for sql, _ in ch.execute_calls]
    assert tables == ["loom.memory_evidence", "loom.memory_extraction_queue"]
    for sql, params in ch.execute_calls:
        assert "DELETE" in sql and "user_id = {uid:String}" in sql
        assert params["uid"] == "alice" and params["org"] == "o"


def test_namespace_scope_deletes_by_namespace_and_is_idempotent() -> None:
    ch = _StubCH([0, 2])
    n = asyncio.run(memory_steps.delete_raw_evidence_by_namespace("o", "ns", ch=ch))
    assert n == 2
    # evidence had nothing → no mutation; queue had rows → one DELETE
    assert len(ch.execute_calls) == 1
    sql, params = ch.execute_calls[0]
    assert "loom.memory_extraction_queue" in sql and "namespace = {ns:String}" in sql
    assert params["ns"] == "ns"


def test_memory_chunks_user_scope_walks_parent_keys() -> None:
    ch = _StubCH([5])
    n = asyncio.run(memory_steps.tombstone_memory_chunks_by_user("o", "alice", ch=ch))
    assert n == 5
    ((read_sql, read_params),) = ch.read_calls
    assert "FROM (SELECT namespace, memory_key FROM loom.memory" in read_sql
    assert read_params == {"org": "o", "uid": "alice"}
    sql, params = ch.execute_calls[0]
    assert "loom.memory_chunks" in sql
    assert "UPDATE tombstoned_at" in sql and "SELECT" not in sql
    assert params["uid"] == "alice" and "now" in params
    assert params["keys"] == [("ns-a", "user_employer")]


def test_memory_chunks_user_scope_is_namespace_qualified() -> None:
    """Extractor keys (`user_employer`) repeat across namespaces: alice's
    `user_employer` in ns A must not tombstone another user's chunks of
    `user_employer` in ns B. The step and the census match the
    (namespace, parent_memory_key) pair: the census through the user's keys
    subquery, the step through the pairs that subquery returns, so the census
    finds exactly what the step erases."""
    ch = _StubCH([1])
    asyncio.run(memory_steps.tombstone_memory_chunks_by_user("o", "alice", ch=ch))
    count_sql, _ = ch.query_one_calls[0]
    mutate_sql, _ = ch.execute_calls[0]
    for sql in (count_sql, mutate_sql):
        assert erasure_predicates.CHUNK_KEYS_IN in sql
    ((read_sql, _),) = ch.read_calls
    assert erasure_predicates.USER_KEYS_SUBQ in read_sql
    (chunks,) = [s for s in erasure_census._CENSUS if s.table == "loom.memory_chunks"]
    assert chunks.user_pred == erasure_predicates.USER_CHUNKS
    assert erasure_predicates.USER_CHUNKS == (
        "(user_id = {uid:String} OR (user_id IN ('-', '') AND "
        f"(namespace, parent_memory_key) IN {erasure_predicates.USER_KEYS_SUBQ}))"
    )


def test_memory_chunks_namespace_scope() -> None:
    ch = _StubCH([2])
    asyncio.run(memory_steps.tombstone_memory_chunks_by_namespace("o", "ns", ch=ch))
    sql, _ = ch.execute_calls[0]
    assert "loom.memory_chunks" in sql and "namespace = {ns:String}" in sql
    assert "tombstoned_at IS NULL" in sql


@pytest.mark.parametrize("scope", ["user_id", "namespace"])
def test_both_sagas_run_the_raw_steps_after_the_memory_tombstone(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    from server.erasure.app import saga as erasure

    names: list[str] = []

    async def _fake_step(
        *, step_name: str, step_fn: Any, completed: set[str], row: dict[str, Any], **kw: Any
    ) -> Any:
        names.append(step_name)
        return None

    monkeypatch.setattr(erasure, "_do_step", _fake_step)
    monkeypatch.setattr("server.erasure.app.backends.all_backends", lambda: [])
    if scope == "user_id":
        asyncio.run(erasure._saga_user_id("o", "alice", completed=set(), row={}))
    else:
        asyncio.run(erasure._saga_namespace("o", "ns", completed=set(), row={}))
    assert "raw_evidence.delete" in names
    assert "memory_chunks.tombstone" in names
    assert names.index("raw_evidence.delete") > names.index("memory.tombstone")
    assert names.index("memory_chunks.tombstone") > names.index("memory.tombstone")
