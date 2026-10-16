"""Unit tests for the document/blob erasure saga steps.

CH-free: a stub client records the SQL it's handed. Verifies the
document-tombstone step reuses the read-filtered `superseded_at` guard and the
blob-erase step deletes the scope's loom.document_blobs rows, then stamps
`blob_erased_at`, both idempotently.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.erasure.app import saga_steps
from server.knowledge.app import originals

_held: list[tuple[str, list[str]]] = []
_hold_originals = originals.hold_originals


@pytest.fixture(autouse=True)
def _claims(monkeypatch: pytest.MonkeyPatch) -> None:
    """The originals claim, recorded instead of taken on the stub client."""

    @asynccontextmanager
    async def hold(org: str, namespaces: Any, **_: Any):
        _held.append((org, list(namespaces)))
        yield

    _held.clear()
    monkeypatch.setattr(originals, "hold_originals", hold)


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


# ----------------------------------------------------------------------
# documents.tombstone — supersede whole-document rows
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_documents_uses_superseded_at_guard(monkeypatch) -> None:
    """The document tombstone reuses the read-filtered `superseded_at`
    column (so the doc stops surfacing) and shares the same WHERE for
    count + mutation."""
    ch = StubCH()
    ch.query_one_returns.extend([{"n": 0}, {"n": 0}, {"n": 4}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    n = await saga_steps._step_tombstone_documents_by_ns("clickhouse", "ns-a")

    assert n == 4
    sel_sql, sel_params = ch.query_one_calls[2]
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
    """Delete the scope's rows of loom.document_blobs, then stamp
    blob_erased_at on the documents still unstamped."""
    ch = StubCH()
    ch.query_one_returns.extend([{"n": 2}, {"n": 2}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    n = await saga_steps._step_erase_blobs_by_ns("clickhouse", "ns-a")

    assert n == 2
    blob_count, stamp_count = ch.query_one_calls
    assert "FROM loom.document_blobs" in blob_count[0] and blob_count[1]["ns"] == "ns-a"
    assert "FROM loom.documents FINAL" in stamp_count[0]
    assert "blob_path != ''" in stamp_count[0] and "blob_erased_at IS NULL" in stamp_count[0]
    # A pre-033 path names a file this step cannot delete: left unstamped, so
    # the census counts it until `loom blobs import` deletes the file.
    assert "position(blob_path, '/') = 0" in stamp_count[0]
    assert "position(blob_path, '/') = 0" in ch.execute_calls[1][0]
    delete_sql, stamp_sql = (sql for sql, _ in ch.execute_calls)
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
    ch.query_one_returns.extend([{"n": 1}, {"n": 1}, {"n": 1}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    n = await saga_steps._step_tombstone_documents_by_ns("o", "ns-a")

    assert n == 1
    assert "loom.document_blobs" in ch.execute_calls[0][0]
    assert "UPDATE blob_erased_at" in ch.execute_calls[1][0]
    assert "UPDATE superseded_at" in ch.execute_calls[2][0]


@pytest.mark.asyncio
async def test_the_documents_step_runs_under_the_originals_claim(monkeypatch) -> None:
    """`loom blobs import` stores an original only under the same claim, after
    re-reading its document, so it cannot store one this step is erasing."""
    ch = StubCH()
    statements_when_held: list[int] = []

    @asynccontextmanager
    async def hold(org: str, namespaces: Any, **_: Any):
        _held.append((org, list(namespaces)))
        statements_when_held.append(len(ch.execute_calls))
        yield
        statements_when_held.append(len(ch.execute_calls))

    monkeypatch.setattr(originals, "hold_originals", hold)
    ch.query_one_returns.extend([{"n": 1}, {"n": 1}, {"n": 1}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    await saga_steps._step_tombstone_documents_by_ns("o", ["a", "b"])

    assert _held == [("o", ["a", "b"])]
    assert statements_when_held == [0, 3]  # every statement inside the claim


@pytest.mark.asyncio
async def test_the_originals_claim_is_held_for_the_block(monkeypatch) -> None:
    from tests.unit.claims_fake import FakeLockTable

    table = FakeLockTable()
    async with _hold_originals("o", ["a"], wait_s=1, ch=table):
        assert list(table.keys) == ["document_originals:o/a"]
    assert table.keys == {}


@pytest.mark.asyncio
async def test_a_lost_originals_claim_stops_the_block(monkeypatch) -> None:
    """The erasure step waits on mutations longer than the claim's TTL: renewed, and
    stopped once another holder has it."""
    from server.core import leases
    from tests.unit.claims_fake import FakeLockTable

    monkeypatch.setattr(originals, "CLAIM_TTL_S", 0.6)
    table = FakeLockTable()
    finished = False
    with pytest.raises(leases.LeaseLost):
        async with _hold_originals("o", ["a"], wait_s=1, ch=table):
            table.keys["document_originals:o/a"]["holder"] = "another-process"
            await asyncio.sleep(3)
            finished = True
    assert not finished


@pytest.mark.asyncio
async def test_purge_deletes_documents_and_chunks(monkeypatch) -> None:
    """A purge census counts every row, so the knowledge steps delete every
    row of the scope, superseded ones included."""
    from server.erasure.store import mutations as erasure_mutations
    from server.erasure.store import steps_memory as memory_steps

    ch = StubCH()
    ch.query_one_returns.extend([{"n": 0}, {"n": 0}, {"n": 3}, {"n": 0}, {"n": 5}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(erasure_mutations, "_default_client", lambda: ch)
    with erasure_mutations.purge_scope(True):
        assert await saga_steps._step_tombstone_documents_by_ns("o", ["a", "b"]) == 3
        assert await memory_steps.supersede_chunks_by_namespace("o", ["a", "b"]) == 5
    counts = [sql for sql, _ in ch.query_one_calls][2:]
    assert "loom.documents" in counts[0] and "superseded_at" not in counts[0]
    assert "loom.chunks" in counts[2] and "doc_superseded_at" not in counts[2]
    deletes = [sql for sql, _ in ch.execute_calls if "DELETE" in sql]
    assert [("loom.documents" in d, "loom.chunks" in d) for d in deletes] == [
        (True, False),
        (False, True),
    ]
    assert all("namespace IN {ns:Array(String)}" in d for d in deletes)


@pytest.mark.asyncio
async def test_purge_keeps_a_row_whose_legacy_file_is_still_on_disk(monkeypatch) -> None:
    """Purge deletes every document row except one whose pre-033 original is a
    file not yet deleted by `loom blobs import`: that row is superseded, so the
    purge census (every row of the scope) still counts it and the saga fails
    closed instead of reaching residue 0 with the file on disk."""
    from server.erasure.store import mutations as erasure_mutations
    from server.erasure.store import steps_memory as memory_steps

    ch = StubCH()
    ch.query_one_returns.extend([{"n": 2}, {"n": 1}])
    monkeypatch.setattr(erasure_mutations, "_default_client", lambda: ch)
    with erasure_mutations.purge_scope(True):
        assert await memory_steps.tombstone_documents_by_namespace("o", "a") == 3
    legacy = "position(blob_path, '/') > 0 AND blob_erased_at IS NULL"
    (delete, _), (keep, _) = ch.execute_calls
    assert "DELETE" in delete and f"NOT ({legacy})" in delete
    assert "UPDATE superseded_at" in keep and legacy in keep and "superseded_at IS NULL" in keep


def test_knowledge_tables_are_census_rows_owned_by_their_steps() -> None:
    """loom.chunks and loom.documents hold knowledge text: the census counts
    them for a namespace scope, and residue re-opens the step that owns it."""
    from server.erasure.store import census as erasure_census

    knowledge = {"loom.chunks", "loom.documents", "loom.document_blobs"}
    assert knowledge <= set(erasure_census.census_tables("namespace"))
    assert not knowledge & set(erasure_census.census_tables("user_id"))
    assert erasure_census.steps_to_reopen({"loom.chunks": 2}, "namespace") == {"chunks.supersede"}
    for table in ("loom.documents", "loom.document_blobs"):
        assert erasure_census.steps_to_reopen({table: 1}, "namespace") == {"documents.tombstone"}
    assert "documents.blob_erase" not in erasure_census.STEP_ONLY_TABLES
