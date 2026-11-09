"""Unit tests for knowledge.upsert content-hash dedup, and for the text writers'
chunks-first order and the cleanup of chunks whose document did not land.

The `stub_writes` fixture stubs the single-row funnel (`documents_repo.insert_one`,
`get_one`), the chunk store's insert, count and cleanup (`insert_chunks`,
`count_active_for_doc`, `retire_versions` and `delete_versions`, recorded) and the
embedder; each test stubs the rest it reaches.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from server.core import pg
from server.core.pg import PostgresUnavailableError


def _content_sha256(text: str, title: str = "") -> str:
    """Mirror of the handler's hash so tests can predict the result."""
    from server.knowledge.app.writes import _content_sha256 as h

    return h(f"{title}\n{text}")


class _FakeEmbedder:
    model_name = "fake-ngram-1024"
    dim = 1024

    async def embed(self, text: str) -> list[float]:
        return [0.0] * self.dim

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


@pytest.fixture
def stub_writes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub out the write side; capture what each repo received."""
    captured: dict[str, Any] = {"doc": None, "chunks": None, "retired": [], "deleted": []}

    from server.knowledge.app import writes as h
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    async def fake_doc_insert(row, *, over=0, retire_older=False, built_from="", ch=None):
        captured["doc"] = row

    async def fake_count(*a, **kw):
        return 0

    async def fake_chunks(rows, *, ch=None):
        captured["chunks"] = rows
        return len(rows)

    async def no_prior(*a, **kw):
        return None

    async def retire_versions(org, ns, versions, **kw):
        captured["retired"].append(list(versions))

    async def delete_versions(org, ns, versions, **kw):
        captured["deleted"].append(list(versions))

    monkeypatch.setattr(knowledge_repo, "retire_versions", retire_versions)
    monkeypatch.setattr(knowledge_repo, "delete_versions", delete_versions)
    monkeypatch.setattr(documents_repo, "insert_one", fake_doc_insert)
    monkeypatch.setattr(documents_repo, "get_one", no_prior)
    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", fake_count)
    monkeypatch.setattr(knowledge_repo, "insert_chunks", fake_chunks)
    monkeypatch.setattr(h, "get_embedder", lambda: _FakeEmbedder())
    return captured


@pytest.mark.asyncio
async def test_doc_content_sha256_persisted(
    monkeypatch: pytest.MonkeyPatch,
    stub_writes: dict[str, Any],
) -> None:
    """The computed doc hash lands in the documents_repo row."""
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app.writes import handle_knowledge_upsert
    from server.knowledge.store import chunks as knowledge_repo

    async def fake_find_doc(*a, **kw):
        return None

    async def fake_count_chunks(*a, **kw):
        return 0

    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", fake_find_doc)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", fake_count_chunks)

    await handle_knowledge_upsert(
        KnowledgeUpsertInput(
            org="clickhouse",
            namespace="finance",
            source_uri="s3://docs/policy.pdf",
            title="Q1 policy",
            content="Deploy on Tuesday and Thursday.",
            tags=[],
        )
    )

    expected_sha = _content_sha256("Deploy on Tuesday and Thursday.", title="Q1 policy")
    assert stub_writes["doc"]["content_sha256"] == expected_sha
    for chunk_row in stub_writes["chunks"]:
        assert "content_sha256" in chunk_row
        assert chunk_row["content_sha256"] != ""


@pytest.mark.asyncio
async def test_duplicate_of_uri_surfaces_cross_uri_match(
    monkeypatch: pytest.MonkeyPatch,
    stub_writes: dict[str, Any],
) -> None:
    """When find_doc_by_content_sha256 returns a hit, duplicate_of_uri
    is populated in the output. Upsert still proceeds (warn-not-block)."""
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app.writes import handle_knowledge_upsert
    from server.knowledge.store import chunks as knowledge_repo

    async def fake_find_doc(org, ns, sha, *, excluding_source_uri="", ch=None):
        assert sha != ""
        # Caller's source_uri is excluded; we return a different URI hit.
        assert excluding_source_uri == "s3://docs/policy-v2.pdf"
        return {
            "source_uri": "s3://docs/policy-v1.pdf",
            "doc_id": "old-doc-id",
            "title": "Q1 policy",
        }

    async def fake_count_chunks(*a, **kw):
        return 0

    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", fake_find_doc)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", fake_count_chunks)

    out = await handle_knowledge_upsert(
        KnowledgeUpsertInput(
            org="clickhouse",
            namespace="finance",
            source_uri="s3://docs/policy-v2.pdf",
            title="Q1 policy",
            content="Same content under a new URI.",
            tags=[],
        )
    )
    assert out.duplicate_of_uri == "s3://docs/policy-v1.pdf"
    assert out.doc_id  # still wrote the new doc
    assert stub_writes["doc"] is not None  # upsert proceeded


@pytest.mark.asyncio
async def test_duplicate_chunk_count_surfaces_boilerplate_overlap(
    monkeypatch: pytest.MonkeyPatch,
    stub_writes: dict[str, Any],
) -> None:
    """duplicate_chunk_count reflects how many newly-inserted chunks
    share content_sha256 with chunks under other source_uris."""
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app.writes import handle_knowledge_upsert
    from server.knowledge.store import chunks as knowledge_repo

    async def fake_find_doc(*a, **kw):
        return None

    captured_hashes = {"v": None}

    async def fake_count_chunks(org, ns, hashes, *, excluding_source_uri="", ch=None):
        captured_hashes["v"] = hashes
        assert excluding_source_uri == "s3://docs/new.md"
        return 3  # imagine 3 chunks were boilerplate overlapping

    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", fake_find_doc)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", fake_count_chunks)

    out = await handle_knowledge_upsert(
        KnowledgeUpsertInput(
            org="clickhouse",
            namespace="finance",
            source_uri="s3://docs/new.md",
            title="",
            content="some content with paragraphs",
            tags=[],
        )
    )
    assert out.duplicate_chunk_count == 3
    assert captured_hashes["v"] is not None
    assert all(h != "" for h in captured_hashes["v"])


@pytest.mark.asyncio
async def test_no_dup_returns_empty_fields(
    monkeypatch: pytest.MonkeyPatch,
    stub_writes: dict[str, Any],
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app.writes import handle_knowledge_upsert
    from server.knowledge.store import chunks as knowledge_repo

    async def fake_find_doc(*a, **kw):
        return None

    async def fake_count_chunks(*a, **kw):
        return 0

    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", fake_find_doc)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", fake_count_chunks)

    out = await handle_knowledge_upsert(
        KnowledgeUpsertInput(
            org="clickhouse",
            namespace="finance",
            source_uri="s3://docs/unique.md",
            title="Unique",
            content="Never been seen before.",
            tags=[],
        )
    )
    assert out.duplicate_of_uri == ""
    assert out.duplicate_chunk_count == 0


@pytest.mark.asyncio
async def test_canonical_form_idempotent_under_whitespace_changes() -> None:
    """`_content_sha256` is invariant to trailing whitespace + collapses
    multiple spaces — two near-identical uploads should produce the
    same hash and dedup against each other."""
    from server.knowledge.app.writes import _content_sha256 as h

    a = h("hello   world\n")
    b = h("hello world")
    c = h("HELLO world")  # case-sensitive (different hash)
    assert a == b
    assert a != c
    assert h("") == ""


@pytest.mark.asyncio
async def test_find_doc_short_circuits_on_empty_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repo helper returns None immediately for empty hash so rows written
    without a content hash and content-less docs don't generate spurious matches.
    """
    from server.knowledge.store import chunks as knowledge_repo

    out = await knowledge_repo.find_doc_by_content_sha256(
        "clickhouse",
        "finance",
        "",
    )
    assert out is None


@pytest.mark.asyncio
async def test_count_chunk_hashes_short_circuits_on_all_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.store import chunks as knowledge_repo

    out = await knowledge_repo.count_matching_chunk_hashes(
        "clickhouse",
        "finance",
        ["", "", ""],
    )
    assert out == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["upsert", "add"])
@pytest.mark.parametrize("refusal", ["moved", "erased"])
async def test_a_refused_commit_retires_this_writes_chunks(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any], op: str, refusal: str
) -> None:
    """Another write of the source committed after this one read it (409), or an erasure
    removed the document it was built from (404): the chunks this write inserted serve
    no published version, so they go before the refusal answers."""
    from server.knowledge.api.models_knowledge import KnowledgeAddInput, KnowledgeUpsertInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.pg_documents import DocumentErased, HeadMoved

    retired: list[list[tuple[str, int]]] = []
    deleted: list[list[tuple[str, int]]] = []
    error = HeadMoved if refusal == "moved" else DocumentErased

    async def moved(row: dict[str, Any], **_: Any) -> None:
        stub_writes["doc"] = row
        raise HeadMoved(row["source_uri"]) if refusal == "moved" else DocumentErased()

    async def supersede(org: str, ns: str, own: list[tuple[str, int]], **kw: Any) -> None:
        retired.append(own)

    async def delete(org: str, ns: str, own: list[tuple[str, int]], **_: Any) -> None:
        deleted.append(own)

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    async def zero(*a: Any, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(documents_repo, "insert_one", moved)
    monkeypatch.setattr(knowledge_repo, "retire_versions", supersede)
    monkeypatch.setattr(knowledge_repo, "delete_versions", delete)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    body = {"org": "o", "namespace": "n", "source_uri": "docs/a.md", "content": "Text."}
    with pytest.raises(error):
        if op == "upsert":
            await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))
        else:
            await writes.handle_knowledge_add(KnowledgeAddInput(**body))
    version = stub_writes["doc"]["version"]
    assert {c["doc_version"] for c in stub_writes["chunks"]} == {version}
    if refusal == "moved":  # retired: another write took the source
        assert (retired, deleted) == ([[("docs/a.md", version)]], [])
    else:  # also deleted: its text and author must not outlive the erasure
        assert (retired, deleted) == ([[("docs/a.md", version)]], [[("docs/a.md", version)]])


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["upsert", "add"])
@pytest.mark.parametrize("live", [True, False])
async def test_a_write_names_the_live_document_it_carries_columns_from(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any], op: str, live: bool
) -> None:
    """A write keeps a live prior's original and author: it names that document, which an
    erasure since refuses. A deleted prior lends nothing, so the write names none."""

    from server.knowledge.api.models_knowledge import KnowledgeAddInput, KnowledgeUpsertInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    prior = documents_repo.DocumentRow(
        org="o",
        namespace="n",
        source_uri="docs/a.md",
        doc_id="prior-id",
        content="Old.",
        version=1,
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        superseded_at=None if live else datetime(2026, 1, 2, tzinfo=UTC),
    )
    named: list[str] = []

    async def insert(row: dict[str, Any], *, built_from: str = "", **_: Any) -> None:
        named.append(built_from)

    async def get_prior(*a: Any, **kw: Any) -> Any:
        return prior

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    async def zero(*a: Any, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(documents_repo, "insert_one", insert)
    monkeypatch.setattr(documents_repo, "get_one", get_prior)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    body = {"org": "o", "namespace": "n", "source_uri": "docs/a.md", "content": "New."}
    if op == "upsert":
        await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))
    else:
        await writes.handle_knowledge_add(KnowledgeAddInput(**body))
    assert named == (["prior-id"] if live else [""])


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["pending", "moved"])
async def test_a_bulk_add_inserts_its_chunks_before_its_commit(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any], outcome: str
) -> None:
    """A commit that answers PUBLICATION_PENDING is published by the delivery worker,
    which publishes the document rows alone: the chunks are in already. A refused commit
    retires them."""
    from server.core.pg_lease import PublicationPending
    from server.knowledge.api.models_knowledge import KnowledgeBulkAddInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.pg_documents import HeadMoved

    order: list[str] = []
    retired: list[list[tuple[str, int]]] = []

    async def chunks(rows: Any, **_: Any) -> int:
        order.append("chunks")
        return len(rows)

    async def commit(rows: Any, **_: Any) -> None:
        order.append("documents")
        raise PublicationPending() if outcome == "pending" else HeadMoved(rows[0]["source_uri"])

    async def supersede(org: str, ns: str, own: list[tuple[str, int]], **kw: Any) -> None:
        retired.append(own)

    async def no_priors(*a: Any, **kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(knowledge_repo, "insert_chunks", chunks)
    monkeypatch.setattr(knowledge_repo, "retire_versions", supersede)
    monkeypatch.setattr(documents_repo, "insert_many", commit)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", no_priors)
    body = {
        "org": "o",
        "namespace": "n",
        "entries": [
            {"source_uri": "a.md", "content": "A."},
            {"source_uri": "b.md", "content": "B."},
        ],
    }
    with pytest.raises(PublicationPending if outcome == "pending" else HeadMoved):
        await writes.handle_knowledge_bulk_add(KnowledgeBulkAddInput(**body))
    assert order == ["chunks", "documents"]
    # A refusal retires both entries' versions in one statement.
    assert [sorted(u for u, _ in r) for r in retired] == (
        [] if outcome == "pending" else [["a.md", "b.md"]]
    )


def test_a_new_version_is_above_the_one_its_writer_read() -> None:
    """A host whose clock runs behind the head's writer still writes above the head."""
    from server.knowledge.store import documents as documents_repo

    ahead = time.time_ns() + 10**12  # a writer 1000 s ahead
    assert documents_repo.version_past(ahead) == ahead + 1
    assert documents_repo.version_past(0) >= time.time_ns() - 10**9


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["moved", "unavailable", "cancel"])
async def test_a_refused_metadata_edit_forgets_the_datasets_it_carried(
    monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """The edit carries the source's datasets to its new version under the source's
    claim, which a text write does not take: refused at the commit, the carried copies
    serve no committed version and go."""
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput
    from server.knowledge.app import datasets, ingest_run, writes
    from server.knowledge.store import datasets as datasets_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.pg_documents import HeadMoved

    live = documents_repo.DocumentRow(
        org="o",
        namespace="n",
        source_uri="a.csv",
        doc_id="d",
        content="x",
        version=7,
        ingestion_id="job",
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    carried: list[int] = []
    forgotten: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def claim(*a: Any) -> Any:
        yield True

    async def get_live(*a: Any, **kw: Any) -> Any:
        return live

    async def carry(org: str, ns: str, uri: str, old: int, new: int) -> None:
        carried.append(new)

    async def moved(row: dict[str, Any], **_: Any) -> None:
        if refusal == "unavailable":  # Postgres refused before the commit: nothing landed
            raise PostgresUnavailableError("lock timeout")
        if refusal == "cancel":  # cancelled before its commit was sent
            raise asyncio.CancelledError()
        raise HeadMoved(row["source_uri"])

    async def forget(org: str, ns: str, uris: list[str], **kw: Any) -> None:
        forgotten.append(kw)

    monkeypatch.setattr(ingest_run, "claim_source", claim)
    monkeypatch.setattr(documents_repo, "get_one", get_live)
    monkeypatch.setattr(datasets, "carry", carry)
    monkeypatch.setattr(documents_repo, "insert_one", moved)
    monkeypatch.setattr(datasets_repo, "forget", forget)
    edit = KnowledgeUpdateMetadataInput(org="o", namespace="n", source_uri="a.csv", title="T")

    expected = {
        "moved": HeadMoved,
        "unavailable": PostgresUnavailableError,
        "cancel": asyncio.CancelledError,
    }[refusal]
    with pytest.raises(expected):
        await writes._edit_ingested(edit, live, {"title": "T"})
    assert len(carried) == 1 and carried[0] > live.version
    assert forgotten == [{"only": carried[0]}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kept"),
    [
        ("unavailable", False),  # Postgres refused before the commit: nothing landed
        ("lost", True),  # the commit's answer was lost: it may have committed
        ("pending", True),  # committed: the delivery worker publishes the row
        ("timeout", False),  # ClickHouse timed out the row: the write answers a 5xx
    ],
)
async def test_an_upsert_keeps_its_chunks_only_when_its_row_may_serve(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any], error: str, kept: bool
) -> None:
    from server.core.errors import BackendTimeoutError
    from server.core.pg_lease import PublicationPending
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    raised = {
        "unavailable": PostgresUnavailableError("lock timeout"),
        "lost": PostgresUnavailableError("answer lost", outcome_unknown=True),
        "pending": PublicationPending(),
        "timeout": BackendTimeoutError("timed out"),
    }[error]
    retired: list[list[tuple[str, int]]] = []

    async def failing(row: dict[str, Any], **_: Any) -> None:
        raise raised

    async def supersede(org: str, ns: str, own: list[tuple[str, int]], **kw: Any) -> None:
        retired.append(own)

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    async def zero(*a: Any, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(documents_repo, "insert_one", failing)
    monkeypatch.setattr(knowledge_repo, "retire_versions", supersede)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    body = {"org": "o", "namespace": "n", "source_uri": "docs/a.md", "content": "Text."}
    with pytest.raises(type(raised)):
        await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))
    assert bool(retired) is not kept


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["upsert", "add", "bulk_add"])
async def test_a_row_postgres_cannot_store_is_refused_before_any_chunk(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any], op: str
) -> None:
    """A Postgres key column holds no NUL: under its authority a write whose source URI
    holds one answers 422 having written nothing, instead of leaving chunks no document
    names. (Other text is kept exactly: `test_documents_repo`.)"""
    from server.core.errors import ValidationError
    from server.knowledge.api.models_knowledge import (
        KnowledgeAddInput,
        KnowledgeBulkAddInput,
        KnowledgeUpsertInput,
    )
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    async def no_priors(*a: Any, **kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", no_priors)
    body = {"org": "o", "namespace": "n", "source_uri": "a\u0000.md", "content": "Text."}
    with pytest.raises(ValidationError):
        if op == "upsert":
            await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))
        elif op == "add":
            await writes.handle_knowledge_add(KnowledgeAddInput(**body))
        else:
            await writes.handle_knowledge_bulk_add(
                KnowledgeBulkAddInput(
                    org="o", namespace="n", entries=[{"source_uri": "a\u0000.md", "content": "Te"}]
                )
            )
    assert stub_writes["chunks"] is None and stub_writes["doc"] is None


@pytest.mark.asyncio
async def test_a_failed_cleanup_keeps_the_writes_own_answer(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any]
) -> None:
    """Another write took the source (409) and ClickHouse then refuses the retirement of
    this write's chunks: the caller still hears the 409."""
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.pg_documents import HeadMoved

    async def moved(row: dict[str, Any], **_: Any) -> None:
        raise HeadMoved(row["source_uri"])

    async def refused(*a: Any, **kw: Any) -> None:
        raise RuntimeError("ClickHouse refused the retirement")

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    async def zero(*a: Any, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(documents_repo, "insert_one", moved)
    monkeypatch.setattr(knowledge_repo, "retire_versions", refused)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    body = {"org": "o", "namespace": "n", "source_uri": "docs/a.md", "content": "Text."}
    with pytest.raises(HeadMoved):
        await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))


@pytest.mark.asyncio
async def test_a_metadata_edit_postgres_cannot_store_carries_no_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import ValidationError
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput
    from server.knowledge.app import datasets, ingest_run, writes
    from server.knowledge.store import documents as documents_repo

    live = documents_repo.DocumentRow(
        org="o",
        namespace="n",
        source_uri="a\x00.csv",  # a source ClickHouse holds, its URI unstorable in a key
        doc_id="d",
        content="x",
        version=7,
        ingestion_id="job",
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    carried: list[int] = []

    @contextlib.asynccontextmanager
    async def claim(*a: Any) -> Any:
        yield True

    async def get_live(*a: Any, **kw: Any) -> Any:
        return live

    async def carry(*a: Any) -> None:
        carried.append(1)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(ingest_run, "claim_source", claim)
    monkeypatch.setattr(documents_repo, "get_one", get_live)
    monkeypatch.setattr(datasets, "carry", carry)
    edit = KnowledgeUpdateMetadataInput(org="o", namespace="n", source_uri="a\x00.csv", title="T")
    with pytest.raises(ValidationError):
        await writes._edit_ingested(edit, live, {"title": "T"})
    assert carried == []


@pytest.mark.asyncio
async def test_a_bulk_add_naming_a_source_twice_is_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any]
) -> None:
    """The head holds one version per source per commit: a second entry for the same
    source would land in ClickHouse as a version Postgres never committed."""
    from server.core.errors import ValidationError
    from server.knowledge.api.models_knowledge import KnowledgeBulkAddInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    calls: list[str] = []

    async def record(*a: Any, **kw: Any) -> None:
        calls.append("write")

    monkeypatch.setattr(knowledge_repo, "insert_chunks", record)
    monkeypatch.setattr(documents_repo, "insert_many", record)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", record)
    body = {
        "org": "o",
        "namespace": "n",
        "entries": [
            {"source_uri": "a.md", "content": "One."},
            {"source_uri": "b.md", "content": "Other."},
            {"source_uri": "a.md", "content": "Two."},
        ],
    }
    with pytest.raises(ValidationError) as refused:
        await writes.handle_knowledge_bulk_add(KnowledgeBulkAddInput(**body))
    assert str(refused.value) == "entries name a source_uri more than once: a.md"
    assert calls == [] and stub_writes["retired"] == []

    seven = [{"source_uri": f"{i}.md", "content": "x"} for i in range(7)] * 2
    with pytest.raises(ValidationError) as refused:
        await writes.handle_knowledge_bulk_add(
            KnowledgeBulkAddInput(org="o", namespace="n", entries=seven)
        )
    assert str(refused.value) == (
        "entries name a source_uri more than once: 0.md, 1.md, 2.md, 3.md, 4.md and 2 more"
    )
    assert calls == []


@pytest.mark.asyncio
async def test_an_add_whose_chunks_fail_commits_no_document(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any]
) -> None:
    """The chunks go in before the commit: a store that refuses them leaves no committed
    document the delivery worker would publish without its chunks, and the write retires
    its own chunk version, since rows of a refused insert may still have landed."""
    from server.knowledge.api.models_knowledge import KnowledgeAddInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    committed: list[Any] = []

    async def chunks_refused(rows: Any, **_: Any) -> int:
        raise RuntimeError("ClickHouse refused the chunks")

    async def commit(row: Any, **_: Any) -> None:
        committed.append(row)

    monkeypatch.setattr(knowledge_repo, "insert_chunks", chunks_refused)
    monkeypatch.setattr(documents_repo, "insert_one", commit)
    body = {"org": "o", "namespace": "n", "source_uri": "a.md", "content": "Text."}
    with pytest.raises(RuntimeError, match="refused the chunks"):
        await writes.handle_knowledge_add(KnowledgeAddInput(**body))
    assert committed == []
    [[(uri, version)]] = stub_writes["retired"]
    assert uri == "a.md" and version > 0 and stub_writes["deleted"] == []


@pytest.mark.asyncio
async def test_a_writers_id_postgres_cannot_store_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any]
) -> None:
    """The row's writer comes from the token, not the request: its id is checked with the
    source URI before the original or a chunk is written."""
    from server.core.errors import ValidationError
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    real = writes.authorship
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(writes, "authorship", lambda i: {**real(i), "user_id": "u\x00"})
    body = {"org": "o", "namespace": "n", "source_uri": "a.md", "content": "Text."}
    with pytest.raises(ValidationError, match="user_id"):
        await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))
    assert stub_writes["chunks"] is None and stub_writes["doc"] is None


@pytest.mark.asyncio
async def test_a_bulk_add_is_refused_before_it_embeds_anything(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any]
) -> None:
    """Every entry's key columns are checked before any is chunked or embedded."""
    from server.core.errors import ValidationError
    from server.knowledge.api.models_knowledge import KnowledgeBulkAddInput
    from server.knowledge.app import writes
    from server.knowledge.store import documents as documents_repo

    embedded: list[int] = []

    async def no_priors(*a: Any, **kw: Any) -> list[Any]:
        return []

    async def embed(embedder: Any, texts: list[str]) -> list[list[float]]:
        embedded.append(len(texts))
        return [[0.0] * 4 for _ in texts]

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", no_priors)
    monkeypatch.setattr(writes, "_embed_inline_batch", embed)
    entries = [{"source_uri": "a.md", "content": "A."}, {"source_uri": "b\x00.md", "content": "B."}]
    with pytest.raises(ValidationError, match="source_uri"):
        await writes.handle_knowledge_bulk_add(
            KnowledgeBulkAddInput(org="o", namespace="n", entries=entries)
        )
    assert embedded == [] and stub_writes["chunks"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("in_flight", [False, True])
async def test_a_cancelled_upsert_retires_its_chunks_unless_its_commit_was_sent(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any], in_flight: bool
) -> None:
    """Cancelled before its commit was sent, the write's chunks serve nothing and go, the
    cleanup shielded from the cancel; cancelled with the commit in flight, it may have
    committed, and they stay."""

    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    retired: list[Any] = []

    async def cancelled(row: dict[str, Any], **_: Any) -> None:
        error = asyncio.CancelledError()
        error.commit_in_flight = in_flight  # type: ignore[attr-defined]
        raise error

    async def supersede(org: str, ns: str, own: list[tuple[str, int]], **kw: Any) -> None:
        retired.append(own)

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    async def zero(*a: Any, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(documents_repo, "insert_one", cancelled)
    monkeypatch.setattr(knowledge_repo, "retire_versions", supersede)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    body = {"org": "o", "namespace": "n", "source_uri": "docs/a.md", "content": "Text."}
    with pytest.raises(asyncio.CancelledError):
        await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))
    assert bool(retired) is not in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["upsert", "add", "bulk_add"])
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_a_chunk_insert_that_may_land_later_is_retired(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any], op: str, failure: str
) -> None:
    """The chunk insert timed out or was cancelled: its rows may still land, with no
    document naming them, so their version is retired before the error answers."""

    from server.core.errors import BackendTimeoutError
    from server.knowledge.api.models_knowledge import (
        KnowledgeAddInput,
        KnowledgeBulkAddInput,
        KnowledgeUpsertInput,
    )
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    retired: list[Any] = []
    error: BaseException = (
        BackendTimeoutError("timed out") if failure == "timeout" else asyncio.CancelledError()
    )

    async def chunks_failed(rows: Any, **_: Any) -> int:
        raise error

    async def supersede(org: str, ns: str, own: list[tuple[str, int]], **kw: Any) -> None:
        retired.append(own)

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    async def zero(*a: Any, **kw: Any) -> int:
        return 0

    async def no_priors(*a: Any, **kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(knowledge_repo, "insert_chunks", chunks_failed)
    monkeypatch.setattr(knowledge_repo, "retire_versions", supersede)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", no_priors)
    body = {"org": "o", "namespace": "n", "source_uri": "a.md", "content": "Text."}
    with pytest.raises(type(error)):
        if op == "upsert":
            await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))
        elif op == "add":
            await writes.handle_knowledge_add(KnowledgeAddInput(**body))
        else:
            await writes.handle_knowledge_bulk_add(
                KnowledgeBulkAddInput(
                    org="o", namespace="n", entries=[{"source_uri": "a.md", "content": "Te"}]
                )
            )
    assert len(retired) == 1 and [u for u, _ in retired[0]] == ["a.md"]
    assert stub_writes["doc"] is None


@pytest.mark.asyncio
async def test_a_bulk_add_over_a_prior_stamped_ahead_stamps_past_it(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any]
) -> None:
    """A prior stamped by a host whose clock runs ahead: the new version still sorts
    after it, so the head and the served row are one."""
    from server.knowledge.api.models_knowledge import KnowledgeBulkAddInput
    from server.knowledge.app import writes
    from server.knowledge.store import documents as documents_repo

    ahead = time.time_ns() + 10**12
    prior = documents_repo.DocumentRow(
        org="o",
        namespace="n",
        source_uri="a.md",
        doc_id="p",
        content="x",
        version=ahead,
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    versions: list[int] = []

    async def priors(*a: Any, **kw: Any) -> list[Any]:
        return [prior]

    async def commit(rows: Any, **_: Any) -> None:
        versions.extend(int(r["version"]) for r in rows)

    monkeypatch.setattr(documents_repo, "get_many_by_uris", priors)
    monkeypatch.setattr(documents_repo, "insert_many", commit)
    entries = [{"source_uri": "a.md", "content": "One."}]
    await writes.handle_knowledge_bulk_add(
        KnowledgeBulkAddInput(org="o", namespace="n", entries=entries)
    )
    assert len(versions) == 1 and versions[0] > ahead


@pytest.mark.asyncio
async def test_an_erased_writes_chunks_are_deleted_even_when_their_retirement_fails(
    monkeypatch: pytest.MonkeyPatch, stub_writes: dict[str, Any]
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app import writes
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.pg_documents import DocumentErased

    deleted: list[Any] = []

    async def erased(row: dict[str, Any], **_: Any) -> None:
        stub_writes["doc"] = row
        raise DocumentErased()

    async def retire_refused(*a: Any, **kw: Any) -> None:
        raise RuntimeError("ClickHouse refused the retirement")

    async def delete(org: str, ns: str, own: list[tuple[str, int]], **_: Any) -> None:
        deleted.append(own)

    async def none(*a: Any, **kw: Any) -> Any:
        return None

    async def zero(*a: Any, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(documents_repo, "insert_one", erased)
    monkeypatch.setattr(knowledge_repo, "retire_versions", retire_refused)
    monkeypatch.setattr(knowledge_repo, "delete_versions", delete)
    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    body = {"org": "o", "namespace": "n", "source_uri": "docs/a.md", "content": "Text."}
    with pytest.raises(DocumentErased):
        await writes.handle_knowledge_upsert(KnowledgeUpsertInput(**body))
    assert deleted == [[("docs/a.md", stub_writes["doc"]["version"])]]


@pytest.mark.parametrize(("read_version", "over"), [(None, 5), (3, 3)])
@pytest.mark.asyncio
async def test_a_rewrite_commits_over_the_version_its_caller_read(
    monkeypatch: pytest.MonkeyPatch,
    stub_writes: dict[str, Any],
    read_version: int | None,
    over: int,
) -> None:
    """A reindex or a rechunk rewrites the content it read: its document row commits
    over that version, not over the newer one the upsert's own read found, so under
    the Postgres authority a write of the source since refuses it (`HeadMoved`) rather
    than being replaced. Without one, the row commits over the version the upsert read."""
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app import writes as h
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.documents import DocumentRow

    async def none(*a, **kw):
        return None

    async def zero(*a, **kw):
        return 0

    newer = DocumentRow(
        org="o",
        namespace="ns",
        source_uri="kb://a",
        doc_id="11111111-1111-1111-1111-111111111111",
        content="new text",
        version=5,
        revision=2,
        ingested_at=datetime(2026, 4, 1, 9, 0, 0),
    )

    async def prior(*a, **kw):
        return newer

    seen: dict[str, Any] = {}

    async def insert_one(row, *, over=0, **kw):
        seen["over"] = over

    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    monkeypatch.setattr(documents_repo, "get_one", prior)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    rewrite = KnowledgeUpsertInput(
        org="o", namespace="ns", source_uri="kb://a", title="A", content="old text", tags=[]
    )
    await h.handle_knowledge_upsert(rewrite, read_version=read_version)
    assert seen["over"] == over


@pytest.mark.asyncio
async def test_an_upsert_retires_older_chunks_even_when_its_count_saw_none(
    monkeypatch: pytest.MonkeyPatch,
    stub_writes: dict[str, Any],
) -> None:
    """The count runs before the prior read, so it can miss chunks a write committed in
    between: the row always asks its publication to retire the older versions."""
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app import writes as h
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.documents import DocumentRow

    async def none(*a, **kw):
        return None

    async def zero(*a, **kw):
        return 0  # stub_writes' count_active_for_doc answers 0 too

    prior = DocumentRow(
        org="o",
        namespace="ns",
        source_uri="kb://a",
        doc_id="11111111-1111-1111-1111-111111111111",
        content="earlier text",
        version=5,
        revision=1,
        ingested_at=datetime(2026, 4, 1, 9, 0, 0),
    )

    async def read_prior(*a, **kw):
        return prior

    seen: dict[str, Any] = {}

    async def insert_one(row, *, over=0, retire_older=False, built_from="", ch=None):
        seen["retire_older"] = retire_older

    monkeypatch.setattr(knowledge_repo, "find_doc_by_content_sha256", none)
    monkeypatch.setattr(knowledge_repo, "count_matching_chunk_hashes", zero)
    monkeypatch.setattr(documents_repo, "get_one", read_prior)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    await h.handle_knowledge_upsert(
        KnowledgeUpsertInput(org="o", namespace="ns", source_uri="kb://a", content="new text")
    )
    assert seen == {"retire_older": True}
