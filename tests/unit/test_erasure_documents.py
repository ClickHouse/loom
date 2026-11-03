"""Unit tests for the document/blob erasure saga steps.

CH-free: a stub client records the SQL it's handed. Verifies the
document-tombstone step reuses the read-filtered `superseded_at` guard and the
blob-erase step deletes the scope's loom.document_blobs rows, then stamps
`blob_erased_at`, both idempotently.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.erasure.app import saga_steps


class StubCH:
    def __init__(self) -> None:
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        if "location != ''" in sql:  # originals in an object store: none here
            return {"stored": 0, "elsewhere": 0}
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        if "system.mutations" in sql:  # the saga's mutation poll: done
            return [
                {
                    "mutation_id": "mutation_1.txt",
                    "is_done": 1,
                    "parts_to_do": 0,
                    "latest_fail_reason": "",
                }
            ]
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))


# ----------------------------------------------------------------------
# documents.tombstone — supersede whole-document rows
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_documents_uses_superseded_at_guard(monkeypatch) -> None:
    """The document tombstone reuses the read-filtered `superseded_at`
    column (so the doc stops surfacing) and shares the same WHERE for
    count + mutation."""
    ch = StubCH()
    ch.query_one_returns.extend([{"n": 0}, {"n": 0}, {"n": 0}, {"n": 4}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    n = await saga_steps._step_tombstone_documents_by_ns("clickhouse", "ns-a")

    assert n == 4
    sel_sql, sel_params = ch.query_one_calls[3]
    assert "FROM loom.documents FINAL" in sel_sql
    assert "superseded_at IS NULL" in sel_sql
    assert sel_params["org"] == "clickhouse"
    assert sel_params["ns"] == "ns-a"
    mut_sql, _ = ch.execute_calls[0]
    assert "ALTER TABLE loom.documents" in mut_sql
    assert "UPDATE superseded_at" in mut_sql
    assert "mutations_sync = 0" in mut_sql


@pytest.mark.asyncio
async def test_tombstone_documents_idempotent_zero_count(monkeypatch) -> None:
    """Re-run with nothing left to supersede → no ALTER fires."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 0})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    n = await saga_steps._step_tombstone_documents_by_ns("clickhouse", "ns-a")

    assert n == 0
    assert ch.execute_calls == []


# ----------------------------------------------------------------------
# documents.blob_erase — delete the originals + stamp marker
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erase_blobs_deletes_and_stamps(monkeypatch) -> None:
    """Delete the scope's rows of loom.document_blobs, stamp blob_erased_at
    on the documents still unstamped, then delete who uploaded them."""
    ch = StubCH()
    ch.query_one_returns.extend([{"n": 2}, {"n": 2}, {"n": 1}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    n = await saga_steps._step_erase_blobs_by_ns("clickhouse", "ns-a")

    assert n == 2
    blob_count, stamp_count, uploads_count = ch.query_one_calls
    assert "FROM loom.document_blobs" in blob_count[0] and blob_count[1]["ns"] == "ns-a"
    assert "FROM loom.documents FINAL" in stamp_count[0]
    assert "blob_path != ''" in stamp_count[0] and "blob_erased_at IS NULL" in stamp_count[0]
    assert "FROM loom.blob_uploads" in uploads_count[0] and uploads_count[1]["ns"] == "ns-a"
    delete_sql, stamp_sql, uploads_sql = (sql for sql, _ in ch.execute_calls)
    assert "ALTER TABLE loom.blob_uploads" in uploads_sql and "DELETE" in uploads_sql
    assert "ALTER TABLE loom.document_blobs" in delete_sql and "DELETE" in delete_sql
    assert "ALTER TABLE loom.documents" in stamp_sql and "UPDATE blob_erased_at" in stamp_sql
    # tagged, awaited mutations like every other saga statement
    assert all("mutations_sync = 0" in sql for sql, _ in ch.execute_calls)
    assert ch.execute_calls[1][1]["erasure_mutation_tag"].startswith("erasure:")
    assert "now" in ch.execute_calls[1][1]


@pytest.mark.asyncio
async def test_erase_blobs_idempotent_no_rows(monkeypatch) -> None:
    """Resumed pass with nothing left → no mutation."""
    ch = StubCH()
    ch.query_one_returns.extend([{"n": 0}, {"n": 0}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    assert await saga_steps._step_erase_blobs_by_ns("clickhouse", "ns-a") == 0
    assert ch.execute_calls == []


@pytest.mark.asyncio
async def test_a_resumed_step_stamps_documents_whose_originals_are_gone(monkeypatch) -> None:
    """A step stopped between the delete and the stamp leaves unstamped
    documents; the resumed pass stamps them though no original is left."""
    ch = StubCH()
    ch.query_one_returns.extend([{"n": 0}, {"n": 3}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    assert await saga_steps._step_erase_blobs_by_ns("o", "ns-a") == 0
    assert ["UPDATE blob_erased_at" in sql for sql, _ in ch.execute_calls] == [True]


@pytest.mark.asyncio
async def test_erase_blobs_binds_a_namespace_list_as_an_array(monkeypatch) -> None:
    """A namespace_list saga hands the step a list. Bound to `{ns:String}` it
    is the literal "['ns-a', 'ns-b']" and matches no row; every statement of
    the step binds an Array."""
    from clickhouse_connect.driver.binding import bind_query

    _, bound = bind_query("namespace = {ns:String}", {"ns": ["ns-a", "ns-b"]})
    assert bound["param_ns"] == "['ns-a', 'ns-b']"  # what the old predicate compared to

    ch = StubCH()
    ch.query_one_returns.extend([{"n": 1}, {"n": 1}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    assert await saga_steps._step_erase_blobs_by_ns("o", ["ns-a", "ns-b"]) == 1
    for sql, params in [*ch.query_one_calls, *ch.execute_calls]:
        assert "namespace IN {ns:Array(String)}" in sql
        assert "{ns:String}" not in sql
        assert params["ns"] == ["ns-a", "ns-b"]


@pytest.mark.asyncio
async def test_tombstone_documents_erases_the_blobs_first(monkeypatch) -> None:
    """The documents step owns the census rows of both tables, so it erases
    the originals — before the rows a purge deletes."""
    ch = StubCH()
    ch.query_one_returns.extend([{"n": 1}, {"n": 1}, {"n": 0}, {"n": 1}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    n = await saga_steps._step_tombstone_documents_by_ns("o", "ns-a")

    assert n == 1
    assert "loom.document_blobs" in ch.execute_calls[0][0]
    assert "UPDATE blob_erased_at" in ch.execute_calls[1][0]
    assert "UPDATE superseded_at" in ch.execute_calls[2][0]


@pytest.mark.asyncio
async def test_purge_deletes_documents_and_chunks(monkeypatch) -> None:
    """A purge census counts every row, so the knowledge steps delete every
    row of the scope, superseded ones included."""
    from server.erasure.store import mutations as erasure_mutations
    from server.erasure.store import steps_memory as memory_steps

    ch = StubCH()
    ch.query_one_returns.extend([{"n": 0}, {"n": 0}, {"n": 0}, {"n": 3}, {"n": 5}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(erasure_mutations, "_default_client", lambda: ch)
    with erasure_mutations.purge_scope(True):
        assert await saga_steps._step_tombstone_documents_by_ns("o", ["a", "b"]) == 3
        assert await memory_steps.supersede_chunks_by_namespace("o", ["a", "b"]) == 5
    counts = [sql for sql, _ in ch.query_one_calls][3:]
    assert "loom.documents" in counts[0] and "superseded_at" not in counts[0]
    assert "loom.chunks" in counts[1] and "doc_superseded_at" not in counts[1]
    deletes = [sql for sql, _ in ch.execute_calls if "DELETE" in sql]
    assert [("loom.documents" in d, "loom.chunks" in d) for d in deletes] == [
        (True, False),
        (False, True),
    ]
    assert all("namespace IN {ns:Array(String)}" in d for d in deletes)


def test_knowledge_tables_are_census_rows_owned_by_their_steps() -> None:
    """loom.chunks and loom.documents hold knowledge text: the census counts
    them for a namespace scope and, by who wrote them, for a user's; an
    original another writer may share is counted by namespace only. Residue
    re-opens the step that owns it."""
    from server.erasure.store import census as erasure_census

    knowledge = {"loom.chunks", "loom.documents", "loom.document_blobs"}
    assert knowledge <= set(erasure_census.census_tables("namespace"))
    assert knowledge & set(erasure_census.census_tables("user_id")) == {
        "loom.chunks",
        "loom.documents",
    }
    assert erasure_census.steps_to_reopen({"loom.documents": 1}, "user_id") == {
        "documents.tombstone"
    }
    assert erasure_census.steps_to_reopen({"loom.chunks": 2}, "namespace") == {"chunks.supersede"}
    for table in ("loom.documents", "loom.document_blobs"):
        assert erasure_census.steps_to_reopen({table: 1}, "namespace") == {"documents.tombstone"}
    assert "documents.blob_erase" not in erasure_census.STEP_ONLY_TABLES
