"""The saga steps that run after the memory steps.
Statement shapes through a stub client — keyed on (org, namespace) with the
list form under a namespace_list saga, the user axis where the table has
one, no FINAL on a plain MergeTree. CH-free; the learnings queue's step runs on
clickhouse-local in test_erasure_learnings_step.py."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.erasure.app import saga_steps
from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_ledgers as ledger_steps


class _StubCH:
    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.counts: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.counts.append((sql, params or {}))
        # count=0 also stands for "no Keeper": the claims table is absent.
        return {"n": self.count, "engine": "KeeperMap" if self.count else ""}

    async def execute(self, sql, params=None):
        self.commands.append((sql, params or {}))

    async def query_all(self, sql, params=None):
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]


def _tables(ch: _StubCH) -> list[str]:
    return [
        sql.split("ALTER TABLE")[1].split()[0] for sql, _ in ch.commands if "ALTER TABLE" in sql
    ]


def test_conversations_step_tombstones_three_tables() -> None:
    ch = _StubCH()
    n = asyncio.run(ledger_steps.tombstone_conversations_by_namespace("o", ["a", "b"], ch=ch))
    assert n == 3
    assert _tables(ch) == [
        "loom.conversations",
        "loom.conversation_threads",
        "loom.conversation_embeddings",
    ]
    by_table = {sql.split("FROM ")[1].split()[0]: sql for sql, _ in ch.counts}
    assert " FINAL" not in by_table["loom.conversations"]  # plain MergeTree: FINAL is code 181
    assert " FINAL" in by_table["loom.conversation_threads"]
    for sql, params in ch.commands:
        assert "namespace IN {ns:Array(String)}" in sql and params["ns"] == ["a", "b"]
    assert all("UPDATE tombstoned_at" in sql for sql, _ in ch.commands)


def test_conversations_user_scope_uses_the_user_column() -> None:
    ch = _StubCH()
    asyncio.run(ledger_steps.tombstone_conversations_by_user("o", "alice", ch=ch))
    assert len(ch.commands) == 3
    for sql, params in ch.commands:
        assert "user_id = {uid:String}" in sql and params["uid"] == "alice"


@pytest.mark.parametrize(
    ("fn", "tables"),
    [
        (ledger_steps.delete_search_trace_log_by_namespace, ["loom.search_trace_log"]),
        (ledger_steps.delete_memory_exposures_by_namespace, ["loom.memory_exposures"]),
        (
            ledger_steps.delete_code_search_trace_by_namespace,
            ["loom.code_search_trace", "loom.code_search_outcome"],
        ),
        (ledger_steps.delete_events_by_namespace, ["loom.events"]),
        (ledger_steps.delete_memory_drops_by_namespace, ["loom.memory_drops"]),
        (
            ledger_steps.delete_learnings_queue_by_namespace,
            ["loom.learnings_supporters", "loom.learnings_queue"],
        ),
        (ledger_steps.delete_consolidation_runs_by_namespace, ["loom.consolidation_runs"]),
        (ledger_steps.delete_projection_failures_by_namespace, ["loom.memory_projection_failures"]),
    ],
)
def test_delete_steps_are_keyed_alter_deletes_over_the_namespace_list(fn, tables) -> None:
    ch = _StubCH(count=3)
    n = asyncio.run(fn("o", ["a", "b"], ch=ch))
    assert n == 3 * len(tables)
    assert _tables(ch) == tables
    for sql, params in ch.commands:
        assert "DELETE" in sql and "UPDATE" not in sql
        assert "(org = {org:String} AND namespace IN {ns:Array(String)})" in sql
        assert params["ns"] == ["a", "b"]
    for sql, _ in ch.counts:
        assert " FINAL" not in sql  # none of these carries a live predicate


def test_the_agent_runs_step_drops_the_runs_claims_too() -> None:
    ch = _StubCH(count=3)
    assert asyncio.run(ledger_steps.delete_agent_runs_by_namespace("o", ["a", "b"], ch=ch)) == 3
    assert _tables(ch) == ["loom.agent_run_claims", "loom.agent_runs"]
    assert "namespace IN {ns:Array(String)}" in ch.commands[0][0]
    assert ch.commands[0][1] == {"org": "o", "ns": ["a", "b"]}
    ch = _StubCH()
    asyncio.run(ledger_steps.delete_agent_runs_by_namespace("o", "a", ch=ch))
    assert "namespace IN {ns:Array(String)}" in ch.commands[0][0]
    assert ch.commands[0][1] == {"org": "o", "ns": ["a"]}
    ch = _StubCH()
    asyncio.run(ledger_steps.delete_agent_runs_by_user("o", "alice", ch=ch))
    assert _tables(ch) == ["loom.agent_run_claims", "loom.agent_runs"]
    assert all("user_id = {uid:String}" in sql for sql, _ in ch.commands)


def test_curation_step_tombstones_three_tables() -> None:
    ch = _StubCH()
    asyncio.run(ledger_steps.tombstone_curation_by_namespace("o", "ns", ch=ch))
    assert _tables(ch) == [
        "loom.operator_edges",
        "loom.conflict_allowlist",
        "loom.memory_categories",
    ]
    assert all("UPDATE tombstoned_at" in sql for sql, _ in ch.commands)


def test_context_blobs_are_tombstoned_in_both_scopes() -> None:
    ch = _StubCH()
    asyncio.run(ledger_steps.tombstone_context_blobs_by_namespace("o", "ns", ch=ch))
    asyncio.run(ledger_steps.tombstone_context_blobs_by_user("o", "alice", ch=ch))
    assert _tables(ch) == ["loom.context_blobs", "loom.context_blobs"]
    assert (
        "namespace = {ns:String}" in ch.commands[0][0]
        and "user_id = {uid:String}" in ch.commands[1][0]
    )


def test_user_scope_delete_steps_skip_the_tables_without_a_user_axis() -> None:
    ch = _StubCH()
    asyncio.run(ledger_steps.delete_code_search_trace_by_user("o", "alice", ch=ch))
    assert _tables(ch) == ["loom.code_search_trace"]  # code_search_outcome has no user_id
    for fn in (
        ledger_steps.delete_memory_exposures_by_user,
        ledger_steps.delete_events_by_user,
        ledger_steps.delete_consolidation_runs_by_user,
    ):
        ch = _StubCH()
        asyncio.run(fn("o", "alice", ch=ch))
        assert len(ch.commands) == 1 and "user_id = {uid:String}" in ch.commands[0][0]


def test_a_purge_turns_the_conversation_tombstones_into_deletes() -> None:
    ch = _StubCH()
    with erasure_mutations.purge_scope(True):
        asyncio.run(ledger_steps.tombstone_conversations_by_namespace("o", ["a"], ch=ch))
    assert all("DELETE" in sql and "tombstoned_at IS NULL" not in sql for sql, _ in ch.commands)
    # conversation_embeddings is on the lightweight list: DELETE FROM, not ALTER
    lw = [sql for sql, _ in ch.commands if sql.lstrip().startswith("DELETE FROM")]
    assert len(lw) == 1 and "loom.conversation_embeddings" in lw[0]


def test_the_sagas_run_every_late_step_in_the_scope_that_owns_it() -> None:
    ns_steps = {name for name, _ in saga_steps._LATE_STEPS_NS}
    user_steps = {name for name, _ in saga_steps._LATE_STEPS_USER}
    owners_ns = set(erasure_census.step_owners("namespace"))
    owners_user = set(erasure_census.step_owners("user_id"))
    assert ns_steps <= owners_ns, ns_steps - owners_ns
    assert user_steps <= owners_user, user_steps - owners_user
    assert user_steps <= ns_steps
    # every census row is owned by a saga step in the scope it is counted in
    for sub in erasure_census._CENSUS:
        if sub.ns_step in ns_steps:
            assert sub.table in erasure_census.census_tables("namespace")
            assert (sub.user_step is not None) == (
                sub.table in erasure_census.census_tables("user_id")
            )
