"""Blobs are content-addressed per namespace: two documents with the same bytes
share one file, so replacing or deleting one must not unlink the other's."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.knowledge.api.models_knowledge import KnowledgeAddInput
from server.knowledge.app import writes as kh
from server.knowledge.store import documents as documents_repo


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _Store:
    """Every call is file I/O (put fsyncs): each must run in a worker thread,
    never on the event loop the other requests share."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def put(self, org: str, ns: str, raw: bytes) -> tuple[str, str]:
        assert not _on_event_loop()
        return "sha", f"{org}/{ns}/new"

    def delete(self, path: str) -> bool:
        assert not _on_event_loop()
        self.deleted.append(path)
        return True


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    import server.storage.blob_store as bs

    s = _Store()
    monkeypatch.setattr(bs, "get_store", lambda: s)
    return s


def _add(uri: str = "doc-a") -> KnowledgeAddInput:
    return KnowledgeAddInput(
        org="o", namespace="n", source_uri=uri, content="x", original_bytes_b64="eA=="
    )


@pytest.mark.parametrize("shared, deleted", [(True, []), (False, ["o/n/old"])])
def test_replacing_bytes_keeps_a_blob_another_document_shares(
    monkeypatch, store, shared, deleted
) -> None:
    asked: list[tuple] = []

    async def _shared(org, ns, path, uri, **_kw):
        asked.append((org, ns, path, uri))
        return shared

    monkeypatch.setattr(documents_repo, "blob_shared_by_another_document", _shared)
    prior = SimpleNamespace(
        blob_path="o/n/old", blob_size=1, blob_content_type="", original_filename=""
    )
    path, *_ = asyncio.run(kh._resolve_blob(_add(), prior))
    assert path == "o/n/new"
    assert asked == [("o", "n", "o/n/old", "doc-a")]
    assert store.deleted == deleted


def test_the_share_check_reads_live_rows_of_other_documents_in_the_namespace() -> None:
    seen: dict = {}

    class _CH:
        async def query_one(self, sql, params=None):
            seen["sql"], seen["params"] = sql, params
            return {"hit": 1}

    assert asyncio.run(
        documents_repo.blob_shared_by_another_document("o", "n", "o/n/b", "doc-a", ch=_CH())
    )
    assert "source_uri != {uri:String}" in seen["sql"] and "superseded_at IS NULL" in seen["sql"]
    assert seen["params"] == {"org": "o", "ns": "n", "bp": "o/n/b", "uri": "doc-a"}


@pytest.mark.parametrize(
    "prior_state, carried",
    [
        ({}, "o/n/old"),  # a live document keeps its download across a text-only write
        ({"superseded_at": "2026-01-01"}, ""),  # deleted: its file was removed
        ({"blob_erased_at": "2026-01-01"}, ""),  # erased: carrying it back is residue
    ],
)
def test_a_text_only_write_carries_a_blob_only_from_a_live_document(
    store, prior_state, carried
) -> None:
    prior = SimpleNamespace(
        blob_path="o/n/old",
        blob_size=7,
        blob_content_type="application/pdf",
        original_filename="a.pdf",
        superseded_at=None,
        blob_erased_at=None,
    )
    for k, v in prior_state.items():
        setattr(prior, k, v)
    text_only = KnowledgeAddInput(org="o", namespace="n", source_uri="doc-a", content="x")
    path, size, _ctype, _name = asyncio.run(kh._resolve_blob(text_only, prior))
    assert path == carried and size == (7 if carried else 0)
    assert store.deleted == []


def test_bulk_delete_releases_the_files_only_its_documents_held(monkeypatch, store) -> None:
    """knowledge.bulk_delete supersedes the documents and then releases their
    files, like knowledge.delete: a file another live document still shares
    stays, a file only the batch held goes — after the superseded rows are
    written, so two batch members sharing one file do not keep it alive."""
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput
    from server.knowledge.store import chunks as chunks_repo

    order: list[str] = []
    docs = [
        SimpleNamespace(source_uri="doc-a", blob_path="o/n/ab"),
        SimpleNamespace(source_uri="doc-b", blob_path="o/n/ab"),
        SimpleNamespace(source_uri="doc-c", blob_path="o/n/kept"),
        SimpleNamespace(source_uri="doc-d", blob_path=""),
    ]

    async def _counts(org, ns, uris, **_kw):
        return dict.fromkeys(uris, 1)

    async def _noop(*a, **kw):
        return None

    async def _docs(*a, **kw):
        return docs

    async def _insert_many(rows, **kw):
        order.append("superseded")

    async def _shared(org, ns, path, uri, **_kw):
        order.append(f"check:{uri}")
        return path == "o/n/kept"  # another live document outside the batch holds it

    monkeypatch.setattr(chunks_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(chunks_repo, "mark_docs_superseded", _noop)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _docs)
    monkeypatch.setattr(documents_repo, "build_document_row", lambda d, **kw: d)
    monkeypatch.setattr(documents_repo, "insert_many", _insert_many)
    monkeypatch.setattr(documents_repo, "blob_shared_by_another_document", _shared)

    out = asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(
                org="o", namespace="n", source_uris=["doc-a", "doc-b", "doc-c", "doc-d"]
            )
        )
    )
    assert out.total_chunks_tombstoned == 4
    assert order == ["superseded", "check:doc-a", "check:doc-b", "check:doc-c"]
    assert store.deleted == ["o/n/ab", "o/n/ab"]


def test_bad_base64_is_a_validation_error(store) -> None:
    from server.core.errors import ValidationError

    bad = KnowledgeAddInput(
        org="o", namespace="n", source_uri="doc-a", content="x", original_bytes_b64="not base64!"
    )
    with pytest.raises(ValidationError):
        asyncio.run(kh._resolve_blob(bad, None))
