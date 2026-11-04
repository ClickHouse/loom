"""Unit tests for knowledge.upsert content-hash dedup.

Boundaries stubbed:
  - documents_repo.insert_one
  - knowledge_repo.{count_active_for_doc, mark_doc_superseded,
                    insert_chunks, find_doc_by_content_sha256,
                    count_matching_chunk_hashes}
  - embedder
"""

from __future__ import annotations

from typing import Any

import pytest


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
    captured: dict[str, Any] = {"doc": None, "chunks": None}

    from server.knowledge.app import writes as h
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    async def fake_doc_insert(row, *, ch=None):
        captured["doc"] = row

    async def fake_count(*a, **kw):
        return 0

    async def fake_supersede(*a, **kw):
        return None

    async def fake_chunks(rows, *, ch=None):
        captured["chunks"] = rows
        return len(rows)

    async def no_prior(*a, **kw):
        return None

    monkeypatch.setattr(documents_repo, "insert_one", fake_doc_insert)
    monkeypatch.setattr(documents_repo, "get_one", no_prior)
    monkeypatch.setattr(knowledge_repo, "count_active_for_doc", fake_count)
    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", fake_supersede)
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
