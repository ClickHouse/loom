"""Unit tests for the document/blob erasure saga steps.

CH-free: a stub client records the SQL it's handed; a stub BlobStore
records which paths were deleted. Verifies the document-tombstone step
reuses the read-filtered `superseded_at` guard and the blob-erase step
enumerates → unlinks → stamps `blob_erased_at`, both idempotently.
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


class StubStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, blob_path: str) -> bool:
        self.deleted.append(blob_path)
        return True


# ----------------------------------------------------------------------
# documents.tombstone — supersede whole-document rows
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_documents_uses_superseded_at_guard(monkeypatch) -> None:
    """The document tombstone reuses the read-filtered `superseded_at`
    column (so the doc stops surfacing) and shares the same WHERE for
    count + mutation."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 4})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    n = await saga_steps._step_tombstone_documents_by_ns("clickhouse", "ns-a")

    assert n == 4
    sel_sql, sel_params = ch.query_one_calls[0]
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
# documents.blob_erase — delete original bytes + stamp marker
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erase_blobs_deletes_and_stamps(monkeypatch) -> None:
    """Enumerate un-erased blobs, unlink each, then stamp blob_erased_at
    with a WHERE mirroring the SELECT guard."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {"source_uri": "doc://a", "blob_path": "clickhouse/ns-a/ab/abc123"},
            {"source_uri": "doc://b", "blob_path": "clickhouse/ns-a/cd/cde456"},
        ]
    )
    store = StubStore()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.storage.blob_store.get_store", lambda: store)

    n = await saga_steps._step_erase_blobs_by_ns("clickhouse", "ns-a")

    assert n == 2
    assert store.deleted == [
        "clickhouse/ns-a/ab/abc123",
        "clickhouse/ns-a/cd/cde456",
    ]
    # SELECT only picks un-erased, non-empty blobs.
    sel_sql, sel_params = ch.query_all_calls[0]
    assert "FROM loom.documents FINAL" in sel_sql
    assert "blob_path != ''" in sel_sql
    assert "blob_erased_at IS NULL" in sel_sql
    assert sel_params["ns"] == ["ns-a"]
    # ALTER stamps the marker with the mirrored guard.
    mut_sql, mut_params = ch.execute_calls[0]
    assert "ALTER TABLE loom.documents" in mut_sql
    assert "UPDATE blob_erased_at" in mut_sql
    assert "blob_erased_at IS NULL" in mut_sql
    # a tagged, awaited mutation like every other saga statement
    assert "mutations_sync = 0" in mut_sql
    assert mut_params["erasure_mutation_tag"].startswith("erasure:")
    assert "now" in mut_params  # explicit timestamp threaded


@pytest.mark.asyncio
async def test_erase_blobs_idempotent_no_rows(monkeypatch) -> None:
    """Resumed pass with no un-erased blobs → no delete, no ALTER."""
    ch = StubCH()
    ch.query_all_returns.append([])
    store = StubStore()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.storage.blob_store.get_store", lambda: store)

    n = await saga_steps._step_erase_blobs_by_ns("clickhouse", "ns-a")

    assert n == 0
    assert store.deleted == []
    assert ch.execute_calls == []


# ----------------------------------------------------------------------
# a namespace_list scope, the purge, and the census row
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erase_blobs_binds_a_namespace_list_as_an_array(monkeypatch) -> None:
    """A namespace_list saga hands the step a list. Bound to `{ns:String}` it
    is the literal "['ns-a', 'ns-b']" and matches no row: the step returned 0
    and the files stayed on disk. SELECT and stamp both bind an Array."""
    from clickhouse_connect.driver.binding import bind_query

    _, bound = bind_query("namespace = {ns:String}", {"ns": ["ns-a", "ns-b"]})
    assert bound["param_ns"] == "['ns-a', 'ns-b']"  # what the old predicate compared to

    ch = StubCH()
    ch.query_all_returns.append([{"source_uri": "doc://a", "blob_path": "o/ns-b/ab/abc"}])
    store = StubStore()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.storage.blob_store.get_store", lambda: store)

    n = await saga_steps._step_erase_blobs_by_ns("o", ["ns-a", "ns-b"])

    assert n == 1 and store.deleted == ["o/ns-b/ab/abc"]
    for sql, params in (ch.query_all_calls[0], ch.execute_calls[0]):
        assert "namespace IN {ns:Array(String)}" in sql
        assert "{ns:String}" not in sql
        assert params["ns"] == ["ns-a", "ns-b"]


@pytest.mark.asyncio
async def test_erase_blobs_unlinks_off_the_event_loop(monkeypatch) -> None:
    import threading

    ch = StubCH()
    ch.query_all_returns.append([{"source_uri": "doc://a", "blob_path": "o/ns-a/ab/abc"}])
    threads: list[int] = []

    class ThreadStore(StubStore):
        def delete(self, blob_path: str) -> bool:
            threads.append(threading.get_ident())
            return super().delete(blob_path)

    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.storage.blob_store.get_store", ThreadStore)

    await saga_steps._step_erase_blobs_by_ns("o", "ns-a")

    assert threads and threading.get_ident() not in threads


@pytest.mark.asyncio
async def test_tombstone_documents_erases_the_blobs_first(monkeypatch) -> None:
    """The documents step owns the census row, blob residue included, so it
    erases the originals — before the rows a purge deletes."""
    ch = StubCH()
    ch.query_all_returns.append([{"source_uri": "doc://a", "blob_path": "o/ns-a/ab/abc"}])
    ch.query_one_returns.append({"n": 1})
    store = StubStore()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.storage.blob_store.get_store", lambda: store)

    n = await saga_steps._step_tombstone_documents_by_ns("o", "ns-a")

    assert n == 1 and store.deleted == ["o/ns-a/ab/abc"]
    assert "UPDATE blob_erased_at" in ch.execute_calls[0][0]
    assert "UPDATE superseded_at" in ch.execute_calls[1][0]


@pytest.mark.asyncio
async def test_purge_deletes_documents_and_chunks(monkeypatch) -> None:
    """A purge census counts every row, so the knowledge steps delete every
    row of the scope, superseded ones included."""
    from server.erasure.store import mutations as erasure_mutations
    from server.erasure.store import steps_memory as memory_steps

    ch = StubCH()
    ch.query_one_returns.extend([{"n": 3}, {"n": 5}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(erasure_mutations, "_default_client", lambda: ch)
    monkeypatch.setattr("server.storage.blob_store.get_store", StubStore)
    with erasure_mutations.purge_scope(True):
        assert await saga_steps._step_tombstone_documents_by_ns("o", ["a", "b"]) == 3
        assert await memory_steps.supersede_chunks_by_namespace("o", ["a", "b"]) == 5
    counts = [sql for sql, _ in ch.query_one_calls]
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
    them for a namespace scope, and residue re-opens the step that owns it."""
    from server.erasure.store import census as erasure_census

    assert {"loom.chunks", "loom.documents"} <= set(erasure_census.census_tables("namespace"))
    assert not {"loom.chunks", "loom.documents"} & set(erasure_census.census_tables("user_id"))
    assert erasure_census.steps_to_reopen({"loom.chunks": 2}, "namespace") == {"chunks.supersede"}
    assert erasure_census.steps_to_reopen({"loom.documents": 1}, "namespace") == {
        "documents.tombstone"
    }
    assert "documents.blob_erase" not in erasure_census.STEP_ONLY_TABLES
