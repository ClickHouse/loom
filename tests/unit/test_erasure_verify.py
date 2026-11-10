"""Verified forgetting: the derived substrates get saga
steps, and the saga's last step is a census that fails on any live row.
CH-free — a stub client records the SQL and feeds back counts."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.erasure.app import saga as h
from server.erasure.app import saga_steps
from server.erasure.store import census as erasure_census
from server.erasure.store import steps_graph as graph_steps
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


class _StubCH:
    """`counts` answer the counts in order; `rows` every read but a system.mutations poll."""

    def __init__(self, counts: list[int], rows: list[dict[str, Any]] | None = None) -> None:
        self.counts = list(counts)
        self.rows = rows or []
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.read_calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return {"n": self.counts.pop(0)} if self.counts else {"n": 0}

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))

    async def query_all(self, sql, params=None):
        if "startsWith(t.name, '_provider_rebuild_')" in sql:
            return []
        if "system.mutations" not in sql:
            self.read_calls.append((sql, params or {}))
            return self.rows
        return [
            {
                "mutation_id": "mutation_1.txt",
                "is_done": 1,
                "parts_to_do": 0,
                "latest_fail_reason": "",
            }
        ]


def _tables(ch: _StubCH) -> list[str]:
    return [sql.split("ALTER TABLE")[1].split()[0] for sql, _ in ch.execute_calls]


def test_source_links_tombstone_both_tables_in_namespace_scope() -> None:
    ch = _StubCH([2, 3])
    n = asyncio.run(graph_steps.tombstone_source_links_by_namespace("o", "ns", ch=ch))
    assert n == 5
    assert _tables(ch) == [
        "loom.memory_belief_sources",
        "loom.memory_chunk_sources",
    ]
    for sql, params in ch.execute_calls:
        assert "UPDATE tombstoned_at" in sql and "namespace = {ns:String}" in sql
        assert "tombstoned_at IS NULL" in sql and params["ns"] == "ns"


def test_user_scope_reaches_link_tables_through_parent_keys() -> None:
    ch = _StubCH([1, 1], rows=[{"namespace": "ns", "memory_key": "k"}])
    asyncio.run(graph_steps.tombstone_source_links_by_user("o", "alice", ch=ch))
    ((read_sql, read_params),) = ch.read_calls
    assert "FROM (SELECT namespace, memory_key FROM loom.memory" in read_sql
    assert "user_id = {uid:String}" in read_sql and read_params["uid"] == "alice"
    assert len(ch.execute_calls) == 2
    for sql, params in ch.execute_calls:
        # Key alone collides across namespaces
        assert "(namespace, memory_key) IN {keys:Array(Tuple(String, String))}" in sql
        assert "SELECT" not in sql and params["keys"] == [("ns", "k")]


def test_graph_links_are_hard_deleted() -> None:
    ch = _StubCH([4, 0])
    n = asyncio.run(graph_steps.delete_graph_links_by_namespace("o", "ns", ch=ch))
    assert n == 4
    # zero-count table → no mutation issued (idempotent re-run is free)
    assert _tables(ch) == ["loom.entity_triple_memories"]
    assert "DELETE" in ch.execute_calls[0][0] and "FINAL" not in ch.query_one_calls[0][0]


def test_records_user_scope_reaches_fields_and_conflicts_before_the_parent() -> None:
    record = "00000000-0000-0000-0000-0000000000d1"
    ch = _StubCH([1, 1, 1, 1], rows=[{"id": record}])
    asyncio.run(graph_steps.tombstone_records_by_user("o", "alice", ch=ch))
    assert _tables(ch) == [
        "loom.memory_record_fields",
        "loom.memory_record_conflicts",
        "loom.memory_records",
        "loom.memory_record_versions",
    ]
    # the user's record ids are read first, then bound
    ((read_sql, read_params),) = ch.read_calls
    assert "FROM (SELECT record_id FROM loom.memory_records" in read_sql
    assert read_params == {"org": "o", "uid": "alice"}
    fields_sql, fields_params = ch.execute_calls[0]
    assert "record_id IN {ids:Array(UUID)}" in fields_sql and fields_params["ids"] == [record]
    conflicts_sql = ch.execute_calls[1][0]
    assert "prior_record_id IN {ids:Array(UUID)} OR incoming_record_id IN" in conflicts_sql
    assert "user_id = {uid:String}" in ch.execute_calls[2][0]
    # The history mirror is deleted, not tombstoned: its view never sees the
    # tombstone's ALTER UPDATE, so a tombstoned mirror row would still serve.
    versions_sql = ch.execute_calls[3][0]
    assert "DELETE" in versions_sql and "user_id = {uid:String}" in versions_sql
    assert "FINAL" not in ch.query_one_calls[3][0]


def test_records_namespace_scope_deletes_the_version_history() -> None:
    ch = _StubCH([1, 1, 1, 1])
    asyncio.run(graph_steps.tombstone_records_by_namespace("o", "ns", ch=ch))
    assert _tables(ch)[-1] == "loom.memory_record_versions"
    assert "DELETE" in ch.execute_calls[3][0] and "UPDATE" not in ch.execute_calls[3][0]


def test_summaries_and_communities() -> None:
    ch = _StubCH([1, 1])
    asyncio.run(graph_steps.tombstone_summaries_by_user("o", "alice", ch=ch))
    assert _tables(ch) == ["loom.session_summaries", "loom.entity_summaries"]
    ch = _StubCH([7])
    n = asyncio.run(graph_steps.delete_entity_communities_by_namespace("o", "ns", ch=ch))
    assert n == 7 and _tables(ch) == ["loom.entity_communities"]


def test_census_covers_every_substrate_and_uses_final_on_soft_deleted_tables() -> None:
    ch = _StubCH([0] * 65)
    out = asyncio.run(erasure_census.residue_census("o", "namespace", "ns", ch=ch))
    assert set(out) == set(erasure_census.census_tables("namespace"))
    # every namespace-scoped substrate, entity, cache, chunk-link and code table
    assert len(out) == 68 and sum(out.values()) == 0
    by_table = {sql.split("FROM ")[1].split()[0]: sql for sql, _ in ch.query_one_calls}
    assert (
        " FINAL" in by_table["loom.memory"] and "tombstoned_at IS NULL" in by_table["loom.memory"]
    )
    assert " FINAL" not in by_table["loom.memory_versions"]
    assert " FINAL" not in by_table["loom.memory_record_versions"]
    # A plain MergeTree with a live predicate never reads FINAL (code 181)
    assert (
        " FINAL" not in by_table["loom.conversations"]
        and "tombstoned_at IS NULL" in by_table["loom.conversations"]
    )
    assert " FINAL" in by_table["loom.conversation_threads"]
    assert " FINAL" not in by_table["loom.search_trace_log"]
    # The knowledge plane: chunks by the readers' live predicate (plain
    # MergeTree, no FINAL); documents live or still holding an original.
    assert (
        " FINAL" not in by_table["loom.chunks"]
        and "doc_superseded_at IS NULL" in (by_table["loom.chunks"])
    )
    assert " FINAL" in by_table["loom.documents"]
    assert "blob_erased_at IS NULL" in by_table["loom.documents"]
    for sql, params in ch.query_one_calls:
        if "FROM loom.idempotency" in sql:  # no namespace column: counted org-wide
            assert "namespace" not in sql
            continue
        assert "namespace = {ns:String}" in sql and params["ns"] == "ns"


def _outbox_row(event_type: str) -> str:
    return (
        "INSERT INTO loom.memory_webhook_outbox (org, namespace, webhook_id, event_id, "
        "event_type, agent, user_id, session_id, memory_key, memory_id, memory_version, actor, "
        f"request_id, erasure_id, occurred_at) VALUES ('o', 'ns', 'wh', 'ev', '{event_type}', "
        "'-', '-', '-', '', '', 0, 'x', '', 'er', now64(3))"
    )


class _OutboxCH(_StubCH):
    """Runs the outbox's count on clickhouse-local after `setup`; every other table is 0."""

    def __init__(self, *setup: str) -> None:
        super().__init__([])
        self.setup = [ddl("memory_webhook_outbox"), *setup]

    async def query_one(self, sql, params=None):
        if "loom.memory_webhook_outbox" not in sql:
            return {"n": 0}
        return run(self.setup, sql, params)[0]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_census_does_not_count_the_sagas_own_erasure_announcements() -> None:
    ch = _OutboxCH(_outbox_row("memory.erased"))
    out = asyncio.run(erasure_census.residue_census("o", "namespace", "ns", ch=ch))
    assert out["loom.memory_webhook_outbox"] == 0
    ch = _OutboxCH(_outbox_row("memory.erased"), _outbox_row("memory.written"))
    out = asyncio.run(erasure_census.residue_census("o", "namespace", "ns", ch=ch))
    assert out["loom.memory_webhook_outbox"] == 1


def test_census_user_scope_skips_namespace_only_tables() -> None:
    ch = _StubCH([0] * 40)
    out = asyncio.run(erasure_census.residue_census("o", "user_id", "alice", ch=ch))
    # the user scope reaches entity_communities, memory_ask_precedents,
    # llm_response_cache and code_answers through the window or memory keys / the owned
    # entities
    assert "loom.entity_edges" not in out and "loom.entity_triples" not in out
    assert "loom.search_trace_log" not in out
    assert {
        "loom.entity_communities",
        "loom.memory_ask_precedents",
        "loom.llm_response_cache",
        "loom.code_answers",
    } <= set(out)
    assert "loom.memory_answers" not in out
    assert len(out) == 53
    assert "loom.blob_uploads" in out  # who uploaded what: the user's go
    # the user's supporter rows, and any bot-rule candidate left with no supporter
    assert {"loom.learnings_queue", "loom.learnings_supporters"} <= set(out)
    assert all(params["uid"] == "alice" for _, params in ch.query_one_calls)


def test_census_propagates_a_read_error() -> None:
    class _BrokenCH(_StubCH):
        async def query_one(self, sql, params=None):
            raise RuntimeError("Code: 241. MEMORY_LIMIT_EXCEEDED")

    with pytest.raises(RuntimeError):
        asyncio.run(erasure_census.residue_census("o", "namespace", "ns", ch=_BrokenCH([])))


def test_verify_step_fails_closed_on_residue_and_fills_the_holder(monkeypatch) -> None:
    async def _census(org, scope_type, scope_value, *, ch=None):
        return {"loom.memory": 0, "loom.memory_belief_sources": 3, "loom.memory_records": 1}

    monkeypatch.setattr(erasure_census, "residue_census", _census)
    holder: dict[str, Any] = {}
    with pytest.raises(saga_steps.ErasureResidueError) as ei:
        asyncio.run(saga_steps._step_verify_residue("o", "namespace", "ns", holder))
    assert ei.value.count == 4
    assert "memory_belief_sources" in str(ei.value) and "loom.memory'" not in str(ei.value)
    assert holder["count"] == 4 and holder["verified_at"] is not None
    cols = h._verify_columns(holder, h._census(holder))
    assert cols["residue_count"] == 4
    assert (
        '"loom.memory_belief_sources": 3' in cols["residue"]
        and 'loom.memory"' not in cols["residue"]
    )


def test_verify_step_passes_on_zero(monkeypatch) -> None:
    async def _census(org, scope_type, scope_value, *, ch=None):
        return {"loom.memory": 0, "loom.memory_versions": 0}

    monkeypatch.setattr(erasure_census, "residue_census", _census)
    holder: dict[str, Any] = {}
    assert asyncio.run(saga_steps._step_verify_residue("o", "user_id", "alice", holder)) == 0
    assert (
        h._verify_columns(holder, h._census(holder))["residue_count"] == 0
        and h._verify_columns({}, h._census({})) == {}
    )


def test_every_saga_step_table_keeps_the_derived_substrates() -> None:
    """verify.residue runs after the table (the step-order test pins it last);
    both tables carry the derived-substrate steps."""
    for table in (saga_steps.USER_STEPS, saga_steps.NAMESPACE_STEPS):
        names = [name for name, _ in table]
        assert len(names) == len(set(names)), names
        assert "verify.residue" not in names
        assert {
            "source_links.tombstone",
            "graph_links.delete",
            "summaries.tombstone",
            "records.tombstone",
        } <= set(names)


def test_every_predicate_column_exists_on_its_table() -> None:
    """The census and the scoped steps are strings; a column that is not on the
    table (memory_record_conflicts has prior_/incoming_record_id, no record_id)
    fails every erasure at the verify step. Check them against schema.sql."""
    import re
    from pathlib import Path

    schema = (Path(__file__).resolve().parents[2] / "schemas" / "schema.sql").read_text()

    def columns(table: str) -> set[str]:
        m = re.search(
            rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\n\((.*?)\n\)\nENGINE", schema, re.S
        )
        assert m, table
        return set(re.findall(r"^\s+`([a-z_0-9]+)`", m.group(1), re.M))

    def strip_subqueries(pred: str) -> str:
        """Remove every `(SELECT …)` with its balanced parentheses (the user
        saga's predicates nest a function call and a UNION inside theirs)."""
        out: list[str] = []
        i = 0
        while i < len(pred):
            if pred.startswith("(SELECT ", i):
                depth = 0
                while i < len(pred):
                    depth += {"(": 1, ")": -1}.get(pred[i], 0)
                    i += 1
                    if depth == 0:
                        break
                continue
            out.append(pred[i])
            i += 1
        return "".join(out)

    for sub in erasure_census._CENSUS:
        table, live, user_pred = sub.table, sub.live, sub.user_pred
        cols = columns(table)
        for pred in (live, sub.ns_pred or "namespace = {ns:String}", user_pred or ""):
            outer = strip_subqueries(pred)
            for ident in re.findall(r"\b([a-z_]+)\s*(?:=|IS|IN)", outer):
                assert ident in cols, (
                    f"{table}: predicate names {ident!r}, columns are {sorted(cols)}"
                )
        assert "org" in cols
    assert "record_id" in columns("loom.memory_record_fields")
    assert "record_id" not in columns("loom.memory_record_conflicts")
    assert {"prior_record_id", "incoming_record_id"} <= columns("loom.memory_record_conflicts")


@pytest.mark.parametrize("scope, value", [("namespace", "ns"), ("user_id", "alice")])
def test_a_cached_query_vector_written_after_its_step_never_fails_the_census(
    scope: str, value: str
) -> None:
    """Any search in the org refills loom.embedding_cache between
    idempotency.delete and verify.residue; the census never counts it."""

    class _Refilled(_StubCH):
        async def query_one(self, sql, params=None):
            self.query_one_calls.append((sql, params or {}))
            return {"n": 1 if "loom.embedding_cache" in sql else 0}

    ch = _Refilled([])
    out = asyncio.run(erasure_census.residue_census("o", scope, value, ch=ch))
    assert "loom.embedding_cache" not in out and sum(out.values()) == 0
    assert not any("loom.embedding_cache" in sql for sql, _ in ch.query_one_calls)
