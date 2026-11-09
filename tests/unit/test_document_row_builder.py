"""loom.documents is ReplacingMergeTree(version): a version row that leaves a
column out resets it for every FINAL reader. Every document write goes through
`documents_repo.build_document_row`, which these tests pin to schema.sql and
check on the write paths."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any

import pytest

from server.knowledge.app import ingest_run
from server.knowledge.app import writes as kh
from server.knowledge.app.writes import _content_sha256
from server.knowledge.store import chunks as knowledge_repo
from server.knowledge.store import datasets as datasets_repo
from server.knowledge.store import documents as documents_repo
from server.knowledge.store.documents import DocumentRow
from tests.unit.schema_ddl import column_names

ERASED = datetime(2026, 5, 1, 12, 0, 0)
INGESTED = datetime(2026, 4, 1, 9, 0, 0)


def _doc(**over: Any) -> DocumentRow:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "source_uri": "doc-a",
        "doc_id": "11111111-1111-1111-1111-111111111111",
        "title": "Old title",
        "description": "desc",
        "content": "body text",
        "tags": ["t"],
        "metadata": '{"k": 1}',
        "version": 1,
        "ingested_at": INGESTED,
        "content_sha256": _content_sha256("Old title\nbody text"),
        "blob_path": "o/n/blob",
        "blob_size": 42,
        "blob_content_type": "application/pdf",
        "original_filename": "a.pdf",
        "blob_erased_at": ERASED,
    }
    base.update(over)
    return DocumentRow(**base)


def test_builder_keys_are_the_schema_columns_a_writer_carries() -> None:
    row = documents_repo.build_document_row(
        org="o", namespace="n", source_uri="u", content="c", version=1
    )
    assert list(row) == column_names("documents", materialized=False)
    assert list(documents_repo.DOCUMENT_COLUMNS) == column_names("documents", materialized=False)


def test_document_row_reads_every_carried_column() -> None:
    assert set(documents_repo.DOCUMENT_COLUMNS) <= set(DocumentRow.model_fields)


def test_builder_carries_an_existing_row_and_recomputes_the_hash() -> None:
    doc = _doc()
    row = documents_repo.build_document_row(doc, version=2, superseded_at=ERASED)
    for col in documents_repo.DOCUMENT_COLUMNS:
        if col not in ("version", "superseded_at"):
            assert row[col] == getattr(doc, col), col
    renamed = documents_repo.build_document_row(doc, title="New", version=3)
    assert renamed["content_sha256"] == _content_sha256("New\nbody text")


def test_builder_refuses_unknown_and_hash_fields() -> None:
    with pytest.raises(ValueError):
        documents_repo.build_document_row(_doc(), version=2, bogus=1)
    with pytest.raises(ValueError):
        documents_repo.build_document_row(_doc(), version=2, content_sha256="x")


def test_the_repo_reads_select_the_hash_and_the_erasure_stamp() -> None:
    seen: list[str] = []

    class _CH:
        async def query_one(self, sql, params=None):
            seen.append(sql)
            return None

        async def query_all(self, sql, params=None):
            seen.append(sql)
            return []

    asyncio.run(documents_repo.get_one("o", "n", "u", ch=_CH()))
    asyncio.run(documents_repo.get_many_by_uris("o", "n", ["u"], ch=_CH()))
    assert len(seen) == 2, seen
    for sql in seen:
        assert "content_sha256" in sql and "blob_erased_at" in sql


def _capture_inserts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def _one(row, *, over=0, retire_older=False, built_from="", ch=None):
        rows.append(row)

    async def _many(batch, *, over=None, retire_older=frozenset(), built_from=(), ch=None):
        rows.extend(batch)
        return len(batch)

    monkeypatch.setattr(documents_repo, "insert_one", _one)
    monkeypatch.setattr(documents_repo, "insert_many", _many)
    return rows


def test_update_metadata_keeps_the_hash_and_the_erasure_stamp(monkeypatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput

    rows = _capture_inserts(monkeypatch)

    async def _get_one(*a, **kw):
        return _doc()

    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    asyncio.run(
        kh.handle_knowledge_update_metadata(
            KnowledgeUpdateMetadataInput(org="o", namespace="n", source_uri="doc-a", title="New")
        )
    )
    [row] = rows
    assert row["content_sha256"] == _content_sha256("New\nbody text")
    assert row["blob_erased_at"] == ERASED
    assert row["ingested_at"] == INGESTED
    assert row["blob_path"] == "o/n/blob"


def test_a_metadata_edit_awaiting_publication_still_reindexes(monkeypatch) -> None:
    """PUBLICATION_PENDING on the edited row says it committed, not to repeat it: the
    reindex the caller asked for runs before the op answers it."""
    from server.core.pg_lease import PublicationPending
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput

    async def _get_one(*a, **kw):
        return _doc()

    written: list[int] = []

    async def _pending(row, **kw):
        written.append(row["version"])
        raise PublicationPending()

    reindexed: list[Any] = []

    async def _upsert(inp, *, read_version=None):
        reindexed.append((inp.title, read_version))
        return type("Out", (), {"chunks_superseded": 2})()

    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "insert_one", _pending)
    monkeypatch.setattr(kh, "handle_knowledge_upsert", _upsert)
    edit = KnowledgeUpdateMetadataInput(
        org="o", namespace="n", source_uri="doc-a", title="New", reindex=True
    )
    with pytest.raises(PublicationPending):
        asyncio.run(kh.handle_knowledge_update_metadata(edit))
    # Over the edit's own committed version: a write after it is refused, not undone.
    assert reindexed == [("New", written[0])]


@pytest.mark.parametrize("case", ["published", "pending", "unchanged"])
def test_a_reindex_refused_after_the_edit_says_the_metadata_was_committed(
    monkeypatch, case: str
) -> None:
    """Another write took the source before the reindex. An edit that committed its row,
    published or still owed, answers a 409 that says so and to ask for the reindex
    again, not the bare 409 that reads as nothing written; a repeat that wrote nothing
    answers the bare one."""
    from server.core.errors import PreconditionFailedError
    from server.core.pg_lease import PublicationPending
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput
    from server.knowledge.store.pg_documents import HeadMoved

    rows = _capture_inserts(monkeypatch)

    async def _get_one(*a, **kw):
        return _doc(title="New") if case == "unchanged" else _doc()

    async def _pending(row, **kw):
        raise PublicationPending()

    async def _moved(inp, *, read_version=None):
        raise HeadMoved(inp.source_uri)

    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    if case == "pending":
        monkeypatch.setattr(documents_repo, "insert_one", _pending)
    monkeypatch.setattr(kh, "handle_knowledge_upsert", _moved)
    edit = KnowledgeUpdateMetadataInput(
        org="o", namespace="n", source_uri="doc-a", title="New", reindex=True
    )
    if case == "unchanged":
        with pytest.raises(HeadMoved):
            asyncio.run(kh.handle_knowledge_update_metadata(edit))
        assert rows == []
        return
    with pytest.raises(PreconditionFailedError, match="metadata was committed") as raised:
        asyncio.run(kh.handle_knowledge_update_metadata(edit))
    assert not isinstance(raised.value, HeadMoved)
    assert len(rows) == (1 if case == "published" else 0)


def test_a_metadata_edit_with_nothing_changed_still_reindexes(monkeypatch) -> None:
    """A repeat of an edit whose reindex did not finish finds the metadata current:
    it writes no row and runs the reindex."""
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput

    rows = _capture_inserts(monkeypatch)

    async def _get_one(*a, **kw):
        return _doc(title="New")

    reindexed: list[Any] = []

    async def _upsert(inp, *, read_version=None):
        reindexed.append(read_version)
        return type("Out", (), {"chunks_superseded": 2})()

    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(kh, "handle_knowledge_upsert", _upsert)
    out = asyncio.run(
        kh.handle_knowledge_update_metadata(
            KnowledgeUpdateMetadataInput(
                org="o", namespace="n", source_uri="doc-a", title="New", reindex=True
            )
        )
    )
    assert rows == [] and reindexed == [_doc().version]  # over the version it read
    assert (out.updated, out.chunks_reindexed) == (False, 2)


def test_delete_tombstones_carry_every_column(monkeypatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput, KnowledgeDeleteInput

    rows = _capture_inserts(monkeypatch)
    doc = _doc()

    async def _count(*a, **kw):
        return 1

    async def _counts(*a, **kw):
        return {"doc-a": 1}

    async def _noop(*a, **kw):
        return None

    async def _get_one(*a, **kw):
        return doc

    async def _get_many(*a, **kw):
        return [doc]

    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    retired: list[dict[str, int]] = []

    async def _forget(org, ns, through, **kw):
        retired.append(dict(through))

    monkeypatch.setattr(datasets_repo, "forget_published", _forget)

    asyncio.run(
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
    )
    asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a"])
        )
    )
    assert len(rows) == 2
    assert retired == [{"doc-a": doc.version}] * 2  # each delete's datasets, at the read version
    for row in rows:
        assert row["superseded_at"] is not None
        for col in ("description", "content_sha256", "blob_path", "blob_size", "blob_erased_at"):
            assert row[col] == getattr(doc, col), col


def test_a_bulk_delete_tombstones_a_live_document_that_holds_no_chunks(monkeypatch) -> None:
    """As `knowledge.delete` does: a live document whose chunks were never embedded, or
    are already retired, is deleted too, not left live with zero counts."""
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput

    rows = _capture_inserts(monkeypatch)

    async def _counts(*a, **kw):
        return {}

    async def _get_many(*a, **kw):
        return [_doc()]

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(datasets_repo, "forget_published", _noop)
    out = asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a"])
        )
    )
    [row] = rows
    assert row["source_uri"] == "doc-a" and row["superseded_at"] is not None
    assert out.total_chunks_tombstoned == 0


def test_a_bulk_delete_naming_a_source_twice_tombstones_it_once(monkeypatch) -> None:
    """A commit holds one row per source: the batch's sources are read and deleted once
    each, and the answer still has one entry per source named."""
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput

    rows = _capture_inserts(monkeypatch)
    asked: list[list[str]] = []

    async def _counts(org, ns, uris, **kw):
        asked.append(list(uris))
        return dict.fromkeys(uris, 2)

    async def _get_many(org, ns, uris, **kw):  # a read that keeps the repeat
        asked.append(list(uris))
        return [_doc(source_uri=u) for u in uris]

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(datasets_repo, "forget_published", _noop)
    out = asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a", "doc-a"])
        )
    )
    [row] = rows
    assert row["source_uri"] == "doc-a" and row["superseded_at"] is not None
    assert asked == [["doc-a"], ["doc-a"]]
    assert [d.source_uri for d in out.deleted] == ["doc-a", "doc-a"]
    assert out.total_chunks_tombstoned == 2


def test_a_delete_retires_older_chunks_even_when_its_count_saw_none(monkeypatch) -> None:
    """The count can miss chunks a write committed after it: a live document's tombstone
    always asks its publication to retire the older versions."""
    from server.knowledge.api.models_knowledge import KnowledgeDeleteInput

    seen: list[bool] = []

    async def _one(row, *, over=0, retire_older=False, built_from="", ch=None):
        seen.append(retire_older)

    async def _zero(*a, **kw):
        return 0

    async def _noop(*a, **kw):
        return None

    async def _get_one(*a, **kw):
        return _doc()

    monkeypatch.setattr(documents_repo, "insert_one", _one)
    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _zero)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(datasets_repo, "forget_published", _noop)
    asyncio.run(
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
    )
    assert seen == [True]


class _FakeEmbedder:
    model_name = "fake"
    dim = 4

    async def embed(self, text: str) -> list[float]:
        return [0.0] * self.dim

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


def test_bulk_add_sets_the_hash_and_keeps_a_prior_blob(monkeypatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeBulkAddInput

    rows = _capture_inserts(monkeypatch)

    async def _chunks(batch, *, ch=None):
        return len(batch)

    async def _get_many(*a, **kw):
        # a live prior: its download carries (an erased or deleted one's would not)
        return [_doc(source_uri="doc-a", blob_erased_at=None)]

    monkeypatch.setattr(knowledge_repo, "insert_chunks", _chunks)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(kh, "get_embedder", lambda: _FakeEmbedder())
    asyncio.run(
        kh.handle_knowledge_bulk_add(
            KnowledgeBulkAddInput(
                org="o",
                namespace="n",
                entries=[
                    {"source_uri": "doc-a", "title": "A", "content": "alpha"},
                    {"source_uri": "doc-b", "title": "B", "content": "beta"},
                ],
            )
        )
    )
    by_uri = {r["source_uri"]: r for r in rows}
    assert by_uri["doc-a"]["content_sha256"] == _content_sha256("A\nalpha")
    assert by_uri["doc-b"]["content_sha256"] == _content_sha256("B\nbeta")
    assert by_uri["doc-a"]["blob_path"] == "o/n/blob"
    assert by_uri["doc-a"]["original_filename"] == "a.pdf"
    assert by_uri["doc-b"]["blob_path"] == ""


def test_the_bulk_ops_commit_over_what_they_read_and_name_it(monkeypatch) -> None:
    """A bulk add commits each source over the version it read and names the live
    priors it carries columns from (an erasure of them refuses it); a bulk delete
    commits over each live document's version and has their tombstones retire them."""
    from server.knowledge.api.models_knowledge import (
        KnowledgeBulkAddInput,
        KnowledgeBulkDeleteInput,
    )

    calls: list[dict[str, Any]] = []

    async def _many(batch, **kw):
        calls.append(kw)
        return len(batch)

    async def _chunks(batch, *, ch=None):
        return len(batch)

    async def _get_many(*a, **kw):
        return [_doc(source_uri="doc-a", blob_erased_at=None)]

    async def _counts(*a, **kw):
        return {"doc-a": 2}

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(documents_repo, "insert_many", _many)
    monkeypatch.setattr(knowledge_repo, "insert_chunks", _chunks)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(datasets_repo, "forget_published", _noop)
    monkeypatch.setattr(kh, "get_embedder", lambda: _FakeEmbedder())
    entries = [
        {"source_uri": "doc-a", "title": "A", "content": "alpha"},
        {"source_uri": "doc-b", "title": "B", "content": "beta"},
    ]
    asyncio.run(
        kh.handle_knowledge_bulk_add(KnowledgeBulkAddInput(org="o", namespace="n", entries=entries))
    )
    asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a", "doc-b"])
        )
    )
    add, delete = calls
    assert add["over"] == {"doc-a": _doc().version}
    assert list(add["built_from"]) == [_doc().doc_id]
    assert delete["over"] == {"doc-a": _doc().version}
    assert delete["retire_older"] == {"doc-a"}


def test_a_delete_of_a_deleted_source_retires_only_its_leftovers(monkeypatch) -> None:
    """The latest row is a tombstone and live chunks remain: only versions up to the
    tombstone's go, so an upsert that inserted newer chunks and has not committed yet
    keeps them, stamped at the delete (under the Postgres authority its own retirement
    may still be owed, and restore finds the delete by that stamp), under the source's
    claim. A source with no document row needs no claim: only its chunks older than the
    orphan age go, since younger ones may be a first write's still to commit."""
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput, KnowledgeDeleteInput
    from server.settings import settings

    rows = _capture_inserts(monkeypatch)
    gone = _doc().model_copy(update={"superseded_at": datetime(2026, 1, 2)})
    retired: list[tuple[str, Any, Any]] = []
    claimed: list[str] = []
    many: list[dict[str, int]] = []

    async def _count(*a, **kw):
        return 2

    async def _counts(org, ns, uris, before_version=None, **kw):
        if before_version == gone.version + 1:  # up to the tombstone: one of doc-a's two
            return {"doc-a": 1}
        return {"doc-new": 1} if before_version is not None else {"doc-a": 2, "doc-new": 1}

    async def _one(org, ns, uri, **kw):
        retired.append((uri, kw.get("before_version"), kw.get("when")))

    async def _many(org, ns, before, **kw):
        many.append(dict(before))

    async def _get_one(org, ns, uri, **kw):
        return gone if uri == "doc-a" else None

    async def _get_many(*a, **kw):
        return [gone]

    async def _noop(*a, **kw):
        return None

    @contextlib.asynccontextmanager
    async def _claim(org, ns, uri):
        claimed.append(uri)
        yield True

    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", _one)
    monkeypatch.setattr(knowledge_repo, "mark_docs_superseded", _many)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(datasets_repo, "forget_published", _noop)
    monkeypatch.setattr(ingest_run, "claim_source", _claim)

    out = asyncio.run(
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
    )
    # Counted by what went: the newer write's chunk, still to commit, stays.
    assert (out.deleted, out.chunks_tombstoned) == (True, 1)
    before_ns = time.time_ns()
    out = asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a", "doc-new"])
        )
    )
    assert rows == []  # nothing live to tombstone
    assert claimed == ["doc-a", "doc-a"]  # the source with no row needs no claim
    (single, bulk_a) = retired
    assert single == bulk_a == ("doc-a", gone.version + 1, gone.superseded_at)
    [orphans] = many  # its old enough chunks: versions below now less the orphan age
    age_ns = (
        max(
            settings.knowledge_orphan_chunk_min_age_seconds,
            settings.knowledge_ingest_max_pass_seconds + kh._CLOCK_SKEW_MARGIN_S,
        )
        * 10**9
    )
    assert list(orphans) == ["doc-new"]
    assert before_ns - age_ns <= orphans["doc-new"] <= time.time_ns() - age_ns
    assert {r.source_uri: r.chunks_tombstoned for r in out.deleted} == {"doc-a": 1, "doc-new": 1}


def test_a_delete_of_a_deleted_source_whose_chunks_are_all_newer_deletes_nothing(
    monkeypatch,
) -> None:
    """Every live chunk is a newer write's still to commit: none is up to the tombstone,
    so the delete retires none and answers that nothing was deleted, as a re-ask does."""
    from server.knowledge.api.models_knowledge import KnowledgeDeleteInput

    gone = _doc().model_copy(update={"superseded_at": datetime(2026, 1, 2)})
    forgotten: list[dict[str, int]] = []

    async def _count(*a, **kw):
        return 2

    async def _counts(org, ns, uris, before_version=None, **kw):
        return {}

    async def _noop(*a, **kw):
        return None

    async def _get_one(*a, **kw):
        return gone

    async def _forget(org, ns, through, **kw):
        forgotten.append(dict(through))

    @contextlib.asynccontextmanager
    async def _claim(org, ns, uri):
        yield True

    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", _noop)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(datasets_repo, "forget_published", _forget)
    monkeypatch.setattr(ingest_run, "claim_source", _claim)
    out = asyncio.run(
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
    )
    assert (out.deleted, out.chunks_tombstoned) == (False, 0)
    assert forgotten == [{"doc-a": gone.version}]  # its datasets still go


def test_a_source_restored_while_its_leftovers_wait_keeps_its_chunks(monkeypatch) -> None:
    """Restore holds the source's claim from its read to its write-back: the delete reads
    the document again under the claim, with sequential consistency, finds it live, and
    retires nothing; it answers that nothing was deleted."""
    from server.core.ch import sequential_reads_active
    from server.knowledge.api.models_knowledge import KnowledgeDeleteInput

    gone = _doc().model_copy(update={"superseded_at": datetime(2026, 1, 2)})
    reads = [gone, _doc()]  # deleted when the delete first reads it, restored by its claim
    retired: list[Any] = []
    held = {"now": False}

    async def _count(*a, **kw):
        return 2

    async def _get_one(*a, **kw):
        if len(reads) == 1:  # the read again: under the claim, sequentially consistent
            assert held["now"] and sequential_reads_active()
        return reads.pop(0)

    async def _one(*a, **kw):
        retired.append(kw)

    async def _noop(*a, **kw):
        return None

    @contextlib.asynccontextmanager
    async def _claim(org, ns, uri):
        held["now"] = True
        yield True
        held["now"] = False

    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", _one)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(datasets_repo, "forget_published", _noop)
    monkeypatch.setattr(ingest_run, "claim_source", _claim)
    out = asyncio.run(
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
    )
    assert retired == [] and reads == []
    assert (out.deleted, out.chunks_tombstoned) == (False, 0)


def test_a_bulk_delete_counts_a_source_restored_meanwhile_as_not_deleted(monkeypatch) -> None:
    """Read again under its claim, a source a restore brought back is live: nothing of it
    is retired, it counts 0, and its datasets stay; the other source's leftovers go."""
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput

    gone_a = _doc(source_uri="doc-a").model_copy(update={"superseded_at": datetime(2026, 1, 2)})
    gone_b = _doc(source_uri="doc-b").model_copy(update={"superseded_at": datetime(2026, 1, 3)})
    retired: list[str] = []
    forgotten: list[dict[str, int]] = []

    async def _counts(*a, **kw):
        return {"doc-a": 2, "doc-b": 3}

    async def _get_many(*a, **kw):
        return [gone_a, gone_b]

    async def _get_one(org, ns, uri, **kw):
        return _doc(source_uri="doc-a") if uri == "doc-a" else gone_b  # doc-a restored

    async def _one(org, ns, uri, **kw):
        retired.append(uri)

    async def _forget(org, ns, through, **kw):
        forgotten.append(dict(through))

    @contextlib.asynccontextmanager
    async def _claim(org, ns, uri):
        yield True

    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", _one)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(datasets_repo, "forget_published", _forget)
    monkeypatch.setattr(ingest_run, "claim_source", _claim)
    out = asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a", "doc-b"])
        )
    )
    assert retired == ["doc-b"]
    assert {r.source_uri: r.chunks_tombstoned for r in out.deleted} == {"doc-a": 0, "doc-b": 3}
    assert out.total_chunks_tombstoned == 3
    assert forgotten == [{"doc-b": gone_b.version}]


@pytest.mark.parametrize("op", ["delete", "bulk_delete"])
def test_a_deletes_first_reads_are_sequentially_consistent(monkeypatch, op: str) -> None:
    """A delete right after a write (a restore) sees it on any replica: its first reads,
    not only the one under the claim, are sequentially consistent."""
    from server.core.ch import sequential_reads_active
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput, KnowledgeDeleteInput

    seen: list[bool] = []

    async def _count(*a, **kw):
        seen.append(sequential_reads_active())
        return 0

    async def _counts(*a, **kw):
        seen.append(sequential_reads_active())
        return {}

    async def _none(*a, **kw):
        seen.append(sequential_reads_active())
        return None

    async def _empty(*a, **kw):
        seen.append(sequential_reads_active())
        return []

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(documents_repo, "get_one", _none)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _empty)
    monkeypatch.setattr(datasets_repo, "forget_published", _noop)
    call = (
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
        if op == "delete"
        else kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a"])
        )
    )
    asyncio.run(call)
    assert seen and all(seen)


@pytest.mark.parametrize("op", ["delete", "bulk_delete"])
def test_a_leftovers_claim_another_write_holds_answers_retryable(monkeypatch, op: str) -> None:
    """The claim not held within its wait: a retryable 503 naming the op, nothing retired."""
    from server.core.errors import BackendUnavailableError
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput, KnowledgeDeleteInput

    gone = _doc().model_copy(update={"superseded_at": datetime(2026, 1, 2)})
    retired: list[Any] = []

    async def _count(*a, **kw):
        return 2

    async def _counts(*a, before_version=None, **kw):
        return {"doc-a": 2}

    async def _get_one(*a, **kw):
        return gone

    async def _get_many(*a, **kw):
        return [gone]

    async def _one(*a, **kw):
        retired.append(kw)

    @contextlib.asynccontextmanager
    async def _busy(org, ns, uri):
        yield False

    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", _one)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(ingest_run, "claim_source", _busy)
    call = (
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
        if op == "delete"
        else kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a"])
        )
    )
    with pytest.raises(BackendUnavailableError) as raised:
        asyncio.run(call)
    assert raised.value.extra["operation"] == f"knowledge.{op}"
    assert retired == []


def test_the_orphan_age_is_never_less_than_an_ingest_pass_and_the_skew_margin(
    monkeypatch,
) -> None:
    """A writer holds chunks before its row for up to an ingest pass: an orphan age set
    below that is raised to it, plus the clock-skew margin."""
    from server.knowledge.api.models_knowledge import KnowledgeDeleteInput
    from server.settings import settings

    bounds: list[int] = []

    async def _count(*a, **kw):
        return 1

    async def _counts(org, ns, uris, *, before_version=None, **kw):
        bounds.append(before_version)
        return {}

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(settings, "knowledge_orphan_chunk_min_age_seconds", 60)
    monkeypatch.setattr(settings, "knowledge_ingest_max_pass_seconds", 900)
    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(documents_repo, "get_one", _none)
    before = time.time_ns()
    asyncio.run(
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="x"))
    )
    age_ns = (900 + kh._CLOCK_SKEW_MARGIN_S) * 10**9
    [cut] = bounds
    assert before - age_ns <= cut <= time.time_ns() - age_ns


@pytest.mark.parametrize("op", ["delete", "bulk_delete"])
def test_a_delete_of_a_source_with_no_row_retires_only_old_orphans(monkeypatch, op: str) -> None:
    """Every writer inserts a new source's chunks before its row: a delete that finds
    chunks and no document row retires only versions older than the orphan age (a failed
    cleanup's, a crash's) and counts only those, so a first write still to commit keeps
    its own; a source with only young chunks answers that nothing was deleted."""
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput, KnowledgeDeleteInput
    from server.settings import settings

    now_ns = time.time_ns()
    hour = 3600 * 10**9
    # Each source's live chunk versions: "old" has an orphan two hours old and a first
    # write's from a second ago; "young" has only the first write's.
    live = {"old": [now_ns - 2 * hour, now_ns - 10**9], "young": [now_ns - 10**9]}

    async def _count(org, ns, uri, **kw):
        return len(live[uri])

    async def _counts(org, ns, uris, *, before_version=None, **kw):
        bound = before_version if before_version is not None else 2**63
        found = {u: sum(v < bound for v in live[u]) for u in uris}
        return {u: n for u, n in found.items() if n}

    async def _many(org, ns, before, **kw):
        for uri, bound in before.items():
            live[uri] = [v for v in live[uri] if v >= bound]

    async def _none(*a, **kw):
        return None

    async def _empty(*a, **kw):
        return []

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(settings, "knowledge_orphan_chunk_min_age_seconds", 3600)
    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(knowledge_repo, "mark_docs_superseded", _many)
    monkeypatch.setattr(documents_repo, "get_one", _none)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _empty)
    monkeypatch.setattr(datasets_repo, "forget_published", _noop)
    if op == "delete":
        old = asyncio.run(
            kh.handle_knowledge_delete(
                KnowledgeDeleteInput(org="o", namespace="n", source_uri="old")
            )
        )
        young = asyncio.run(
            kh.handle_knowledge_delete(
                KnowledgeDeleteInput(org="o", namespace="n", source_uri="young")
            )
        )
        answers = {"old": (old.deleted, old.chunks_tombstoned)}
        answers["young"] = (young.deleted, young.chunks_tombstoned)
        assert answers == {"old": (True, 1), "young": (False, 0)}
    else:
        out = asyncio.run(
            kh.handle_knowledge_bulk_delete(
                KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["old", "young"])
            )
        )
        assert {r.source_uri: r.chunks_tombstoned for r in out.deleted} == {"old": 1, "young": 0}
        assert out.total_chunks_tombstoned == 1
    # The orphan went; each first write's chunk, a second old, stays live.
    assert live == {"old": [now_ns - 10**9], "young": [now_ns - 10**9]}


def test_a_delete_left_to_the_delivery_worker_forgets_its_datasets(monkeypatch) -> None:
    """The tombstone is committed and its publication pending: the caller is told not to
    repeat the delete, so its datasets go now, as on a delete that published."""
    from server.core.pg_lease import PublicationPending
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput, KnowledgeDeleteInput

    doc = _doc()
    forgotten: list[dict[str, int]] = []

    async def _pending(*a, **kw):
        raise PublicationPending()

    async def _count(*a, **kw):
        return 1

    async def _counts(*a, **kw):
        return {"doc-a": 1}

    async def _noop(*a, **kw):
        return None

    async def _get_one(*a, **kw):
        return doc

    async def _get_many(*a, **kw):
        return [doc]

    async def _forget(org, ns, through, **kw):
        forgotten.append(dict(through))

    monkeypatch.setattr(documents_repo, "insert_one", _pending)
    monkeypatch.setattr(documents_repo, "insert_many", _pending)
    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", _count)
    monkeypatch.setattr(knowledge_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _get_many)
    monkeypatch.setattr(datasets_repo, "forget_published", _forget)
    with pytest.raises(PublicationPending):
        asyncio.run(
            kh.handle_knowledge_delete(
                KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a")
            )
        )
    with pytest.raises(PublicationPending):
        asyncio.run(
            kh.handle_knowledge_bulk_delete(
                KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a"])
            )
        )
    assert forgotten == [{"doc-a": doc.version}] * 2
