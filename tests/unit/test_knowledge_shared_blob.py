"""Document originals leave only through the sweep: no knowledge write deletes
one inline. A re-upload that fails after storing its bytes (the embedder down,
the insert refused) keeps the live document's original, and a delete costs no
mutation per request; the sweep reclaims what no live document names."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from server.knowledge.api.models_knowledge import KnowledgeAddInput
from server.knowledge.app import writes as kh
from server.knowledge.store import documents as documents_repo


class _Store:
    """The originals table: `put` returns the new original's sha256."""

    def __init__(self) -> None:
        self.put_calls = 0

    async def put(self, org: str, ns: str, raw: bytes, **_kw: object) -> str:
        self.put_calls += 1
        return "new"

    async def delete(self, *a: object, **kw: object) -> int:
        raise AssertionError("a knowledge write deleted an original inline")


class _NoCH:
    """Any statement a test did not patch: a release would read or delete here."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        async def _call(*a, **kw):
            self.calls.append(name)
            return None

        return _call


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Store]:
    from server.knowledge.store import blobs

    s = _Store()
    s.ch = _NoCH()  # type: ignore[attr-defined]
    monkeypatch.setattr(blobs, "put", s.put)
    monkeypatch.setattr(blobs, "delete", s.delete)
    monkeypatch.setattr("server.core.ch.get_client", lambda: s.ch)
    yield s
    assert s.ch.calls == []  # type: ignore[attr-defined]


def _add(uri: str = "doc-a") -> KnowledgeAddInput:
    return KnowledgeAddInput(
        org="o", namespace="n", source_uri=uri, content="x", original_bytes_b64="eA=="
    )


def _prior(**over: object) -> SimpleNamespace:
    row = SimpleNamespace(
        blob_path="old",
        blob_size=7,
        blob_content_type="application/pdf",
        original_filename="a.pdf",
        superseded_at=None,
        blob_erased_at=None,
        content="an earlier text",  # not the new one: the writer becomes its author
        user_id="-",  # written by no end user: its original carries to any writer
        revision=1,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def test_replacing_bytes_leaves_the_prior_original_to_the_sweep(store) -> None:
    path, *_ = asyncio.run(kh._resolve_blob(_add(), _prior(), uploader="-"))
    assert path == "new" and store.put_calls == 1


def test_a_re_upload_that_fails_after_its_put_keeps_the_live_download(monkeypatch, store) -> None:
    """The document insert fails after the new bytes are stored: the live row
    still names the old original, and nothing deleted it."""
    from server.knowledge.store import chunks as chunks_repo

    async def _none(*a, **kw):
        return None

    async def _one(*a, **kw):
        return 1

    async def _get_one(*a, **kw):
        return _prior()

    async def _refused(*a, **kw):
        raise RuntimeError("insert refused")

    monkeypatch.setattr(chunks_repo, "find_doc_by_content_sha256", _none)
    monkeypatch.setattr(chunks_repo, "count_active_for_doc", _one)
    monkeypatch.setattr(chunks_repo, "mark_doc_superseded", _none)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "insert_one", _refused)
    monkeypatch.setattr(kh, "get_embedder", lambda: SimpleNamespace(model_name="f", dim=4))
    with pytest.raises(RuntimeError, match="insert refused"):
        asyncio.run(kh.handle_knowledge_upsert(_add()))
    assert store.put_calls == 1  # stored, then the write failed: no release ran


def test_an_upload_the_store_refuses_leaves_the_current_version_searchable(
    monkeypatch, store
) -> None:
    """The bucket is down or busy: the original is stored before the current
    version's chunks are retired, so a refused upload retires nothing."""
    from server.core.object_store import ObjectStoreError
    from server.knowledge.store import blobs
    from server.knowledge.store import chunks as chunks_repo

    retired: list[str] = []

    async def _none(*a, **kw):
        return None

    async def _one(*a, **kw):
        return 1

    async def _get_one(*a, **kw):
        return _prior()

    async def _retire(*a, **kw):
        retired.append(a[2])

    async def _busy(*a, **kw):
        raise ObjectStoreError("object store write failed (SlowDown)", operation="write")

    monkeypatch.setattr(chunks_repo, "find_doc_by_content_sha256", _none)
    monkeypatch.setattr(chunks_repo, "count_active_for_doc", _one)
    monkeypatch.setattr(chunks_repo, "mark_doc_superseded", _retire)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(blobs, "put", _busy)
    monkeypatch.setattr(kh, "get_embedder", lambda: SimpleNamespace(model_name="f", dim=4))
    with pytest.raises(ObjectStoreError):
        asyncio.run(kh.handle_knowledge_upsert(_add()))
    assert retired == []


@pytest.mark.parametrize(
    "prior_state, carried",
    [
        ({}, "old"),  # a live document keeps its download across a text-only write
        ({"superseded_at": "2026-01-01"}, ""),  # deleted: its original is the sweep's
        ({"blob_erased_at": "2026-01-01"}, ""),  # erased: carrying it back is residue
    ],
)
def test_a_text_only_write_carries_a_blob_only_from_a_live_document(
    store, prior_state, carried
) -> None:
    text_only = KnowledgeAddInput(org="o", namespace="n", source_uri="doc-a", content="x")
    path, size, _ctype, _name = asyncio.run(
        kh._resolve_blob(text_only, _prior(**prior_state), uploader="-")
    )
    assert path == carried and size == (7 if carried else 0)


def test_delete_and_bulk_delete_supersede_without_touching_the_originals(
    monkeypatch, store
) -> None:
    from server.knowledge.api.models_knowledge import (
        KnowledgeBulkDeleteInput,
        KnowledgeDeleteInput,
    )
    from server.knowledge.store import chunks as chunks_repo
    from server.knowledge.store import datasets as datasets_repo

    written: list[object] = []
    forgotten: list[dict[str, int]] = []
    docs = [
        _prior(source_uri="doc-a", version=10),
        _prior(source_uri="doc-b", blob_path="ab", version=20),
    ]

    async def _counts(org, ns, uris, **_kw):
        return dict.fromkeys(uris, 1)

    async def _one(*a, **kw):
        return 1

    async def _noop(*a, **kw):
        return None

    async def _docs(*a, **kw):
        return docs

    async def _get_one(*a, **kw):
        return docs[0]

    async def _insert(rows, **kw):
        written.append(rows)

    async def _forget(org, ns, through, **kw):
        forgotten.append(dict(through))

    monkeypatch.setattr(datasets_repo, "forget_published", _forget)
    monkeypatch.setattr(chunks_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(chunks_repo, "count_active_for_doc", _one)
    monkeypatch.setattr(chunks_repo, "mark_docs_superseded", _noop)
    monkeypatch.setattr(chunks_repo, "mark_doc_superseded", _noop)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _docs)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(documents_repo, "build_document_row", lambda d, **kw: d)
    monkeypatch.setattr(documents_repo, "insert_many", _insert)
    monkeypatch.setattr(documents_repo, "insert_one", _insert)

    asyncio.run(
        kh.handle_knowledge_delete(KnowledgeDeleteInput(org="o", namespace="n", source_uri="doc-a"))
    )
    asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a", "doc-b"])
        )
    )
    assert len(written) == 2  # the superseded rows, and no delete of an original
    # And the datasets of the versions they retired.
    v = docs[0].version, docs[1].version
    assert forgotten == [{"doc-a": v[0]}, {"doc-a": v[0], "doc-b": v[1]}]


def test_bad_base64_is_a_validation_error(store) -> None:
    from server.core.errors import ValidationError

    bad = KnowledgeAddInput(
        org="o", namespace="n", source_uri="doc-a", content="x", original_bytes_b64="not base64!"
    )
    with pytest.raises(ValidationError):
        asyncio.run(kh._resolve_blob(bad, None, uploader="-"))


def test_an_end_user_s_upload_is_recorded_before_its_bytes() -> None:
    """Erasure finds the user's originals by this record; a write with no end
    user records none."""
    from server.knowledge.store import blobs

    class _CH:
        def __init__(self) -> None:
            self.inserts: list[tuple[str, list[dict[str, object]]]] = []

        async def insert(self, table: str, rows: list[dict[str, object]], **_k: object) -> None:
            self.inserts.append((table, rows))

    ch = _CH()
    sha = asyncio.run(blobs.put("o", "n", b"bytes", uploader="u-1", ch=ch))  # type: ignore[arg-type]
    assert [t for t, _ in ch.inserts] == [blobs.UPLOADS, blobs.TABLE]
    assert ch.inserts[0][1] == [{"org": "o", "namespace": "n", "sha256": sha, "user_id": "u-1"}]
    ch = _CH()
    asyncio.run(blobs.put("o", "n", b"bytes", uploader="-", ch=ch))  # type: ignore[arg-type]
    assert [t for t, _ in ch.inserts] == [blobs.TABLE]


def test_a_bulk_delete_of_sources_already_deleted_still_deletes_their_datasets(
    monkeypatch, store
) -> None:
    """Nothing live to tombstone, yet a snapshot an approval or an older build
    left goes: the datasets are read off each source's latest row, superseded
    or not."""
    from server.knowledge.api.models_knowledge import KnowledgeBulkDeleteInput
    from server.knowledge.store import chunks as chunks_repo
    from server.knowledge.store import datasets as datasets_repo

    gone = [_prior(source_uri="doc-a", version=10, superseded_at="2026-01-01")]
    forgotten: list[dict[str, int]] = []

    async def _counts(org, ns, uris, **_kw):
        return dict.fromkeys(uris, 0)

    async def _docs(org, ns, uris, **kw):
        return gone if kw.get("include_superseded") else []

    async def _forget(org, ns, through, **kw):
        forgotten.append(dict(through))

    monkeypatch.setattr(chunks_repo, "count_active_by_uri", _counts)
    monkeypatch.setattr(documents_repo, "get_many_by_uris", _docs)
    monkeypatch.setattr(datasets_repo, "forget_published", _forget)
    out = asyncio.run(
        kh.handle_knowledge_bulk_delete(
            KnowledgeBulkDeleteInput(org="o", namespace="n", source_uris=["doc-a", "doc-b"])
        )
    )
    assert out.total_chunks_tombstoned == 0
    assert forgotten == [{"doc-a": 10}]
