"""Knowledge reads under the Postgres authority serve only the chunks their source's head
serves (`server.knowledge.domain.serving`, `server.knowledge.app.pg_serving`): not an older
version's still being retired, a deleted document's, nor a write's not committed."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.core import pg
from server.knowledge.app import pg_serving
from server.knowledge.app.search import ChunkHit
from server.knowledge.domain.serving import Serving, serves
from server.knowledge.store import pg_documents

_HEAD = Serving(deleted=False, revision=3, chunks_revision=3, chunks_version=30)


def _hit(namespace: str, uri: str, revision: int, version: int, idx: int = 0) -> ChunkHit:
    """A chunk as the check reads it: its source, revision and version."""
    return ChunkHit(namespace, uri, idx, "", [], 0.1, revision=revision, doc_version=version)


@pytest.mark.parametrize(
    ("head", "revision", "doc_version", "served"),
    [
        (None, 1, 10, True),  # no head: written before heads, ClickHouse says
        (_HEAD, 3, 30, True),  # the head's own chunks
        (_HEAD, 2, 20, False),  # an older revision still being retired
        (_HEAD, 4, 40, False),  # a write not committed (or refused)
        (_HEAD, 3, 31, False),  # the same revision, another write's (a refused racer)
        (_HEAD, 0, 30, True),  # a chunk that recorded no revision, at the head's version
        (_HEAD, 0, 20, False),
        (_HEAD._replace(deleted=True), 3, 30, False),  # deleted, its tombstone owed
        # A build that records none (or a commit that inserted none) moved the revision:
        # the recorded version says nothing about it, and the revision alone decides.
        (_HEAD._replace(revision=4), 4, 40, True),
        (_HEAD._replace(revision=4), 3, 30, False),
        (Serving(False, 3, None, None), 3, 99, True),
    ],
)
def test_a_chunk_is_served_by_its_heads_revision_and_recorded_version(
    head: Serving | None, revision: int, doc_version: int, served: bool
) -> None:
    assert serves(head, revision, doc_version) is served


@pytest.mark.asyncio
async def test_without_the_postgres_authority_nothing_is_read_or_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*a: Any) -> Any:
        raise AssertionError("no Postgres read without its authority")

    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(pg_documents, "serving", never)
    assert await pg_serving.stale("o", [_hit("n", "a.md", 9, 9)]) == [False]


@pytest.mark.asyncio
async def test_one_read_judges_a_page_across_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[list[tuple[str, str]]] = []

    async def heads(org: str, sources: list[tuple[str, str]]) -> dict[Any, Serving]:
        asked.append(list(sources))
        return {("n1", "a.md"): _HEAD, ("n2", "a.md"): _HEAD._replace(deleted=True)}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", heads)
    refs = [
        _hit("n1", "a.md", 3, 30),
        _hit("n2", "a.md", 3, 30),
        _hit("n1", "b.md", 1, 1),  # no head
    ]
    assert await pg_serving.stale("o", refs) == [False, True, False]
    assert len(asked) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("page", "deeper"), [(2, True), (4, False)])
async def test_a_full_page_with_stale_hits_is_fetched_once_more_twice_as_deep(
    monkeypatch: pytest.MonkeyPatch, page: int, deeper: bool
) -> None:
    """A page of `k` that held a stale hit may have more served ones past it; a shorter
    one holds all there is."""
    ranked = [_hit("n", "a.md", 2, 20), _hit("n", "a.md", 3, 30), _hit("n", "b.md", 1, 1)]
    fetched: list[int] = []

    async def fetch(k: int) -> list[ChunkHit]:
        fetched.append(k)
        return ranked[:k]

    async def heads(org: str, sources: Any) -> dict[Any, Serving]:
        return {("n", "a.md"): _HEAD}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", heads)
    kept, dropped = await pg_serving.served_search("o", fetch, page)
    assert dropped == 1 and kept == ranked[1:]
    assert fetched == ([page, page * 2] if deeper else [page])


@pytest.mark.asyncio
async def test_a_source_with_no_head_keeps_one_hit_per_chunk_and_the_page_refills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every version of a source written before heads is served: the best ranked of a
    chunk's versions stands for it, and the one left out counts toward the refill (it is
    not stale)."""
    ranked = [
        ChunkHit("n", "a.md", 0, "newer", [], 0.1, revision=2, doc_version=20),
        ChunkHit("n", "a.md", 0, "older", [], 0.2, revision=1, doc_version=10),
        ChunkHit("n", "b.md", 0, "other", [], 0.3, revision=1, doc_version=1),
    ]
    fetched: list[int] = []

    async def fetch(k: int) -> list[ChunkHit]:
        fetched.append(k)
        return ranked[:k]

    async def heads(org: str, sources: Any) -> dict[Any, Serving]:
        return {}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", heads)
    kept, dropped = await pg_serving.served_search("o", fetch, 2)
    assert [h.content for h in kept] == ["newer", "other"] and dropped == 0
    assert fetched == [2, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
async def test_a_search_leaves_out_the_stale_version_and_counts_it(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Two versions of a source read as live (the older one's retirement owed): the
    page shows the head's, and `stale_dropped` counts the other. In hybrid mode the legs
    are fused first: merged into one candidate the two versions would be judged as the
    stale one (the leg lists it last), and the served chunk would be lost."""
    from server.core import ch as ch_mod
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import bm25, search
    from server.knowledge.app import knowledge_search as ks

    class _Embedder:
        model_name = "m"

        async def embed(self, text: str) -> list[float]:
            return [1.0]

    hits = [
        ChunkHit("n", "a.md", 0, "new text", [], distance=0.1, revision=3, doc_version=30),
        ChunkHit("n", "a.md", 0, "old text", [], distance=0.2, revision=2, doc_version=20),
        ChunkHit("n", "b.md", 0, "other", [], distance=0.3, revision=1, doc_version=1),
    ]

    async def legs(q: Any) -> list[ChunkHit]:
        return hits[: q.top_k]

    async def no_hits(q: Any) -> list[ChunkHit]:
        return []

    async def stats(*a: Any, **kw: Any) -> tuple[float, list[float]]:
        return 1.0, [1.0]

    async def first(input: Any, namespaces: list[str], run: Any) -> Any:
        return await run(namespaces)

    async def named(input: Any) -> list[str]:
        return ["n"]

    async def heads(org: str, sources: Any) -> dict[Any, Serving]:
        return {("n", "a.md"): _HEAD}

    monkeypatch.setattr(ks, "get_embedder", _Embedder)
    monkeypatch.setattr(search, "hybrid_search", legs)
    monkeypatch.setattr(search, "lexical_search", no_hits)
    monkeypatch.setattr(search, "keys_search", no_hits)
    monkeypatch.setattr(bm25, "lexical_stats", stats)
    monkeypatch.setattr(ch_mod, "get_client", lambda: None)
    monkeypatch.setattr(ks, "_search_private_first", first)
    monkeypatch.setattr(ks, "_named_namespaces", named)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", heads)
    out = await ks.handle_knowledge_search(
        KnowledgeSearchInput(
            org="o", namespace="n", namespaces=["n"], query="text", search_mode=mode
        )
    )
    assert [r.content for r in out.results] == ["new text", "other"]
    assert out.stale_dropped == 1


@pytest.mark.parametrize("by_version", [True, False])
def test_fusion_keeps_two_versions_of_a_chunk_apart_for_the_check(by_version: bool) -> None:
    """Under the Postgres authority the stale and the served version of one chunk are two
    candidates: merged, the page would show one version's text under the other's identity.
    Under the ClickHouse one fusion merges them into one hit, as it always has."""
    from server.knowledge.app.search import fuse_rrf

    old = ChunkHit("n", "a.md", 0, "old", [], distance=0.1, revision=2, doc_version=20)
    new = ChunkHit("n", "a.md", 0, "new", [], distance=0.2, revision=3, doc_version=30)
    fused = fuse_rrf([old, new], [], top_k=5, by_version=by_version)
    assert len(fused) == (2 if by_version else 1)
    if by_version:
        assert sorted((h.content, h.doc_version) for h in fused) == [("new", 30), ("old", 20)]


@pytest.mark.asyncio
async def test_a_search_serves_one_hit_per_chunk_of_a_source_with_no_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source written before heads serves every version ClickHouse holds: the page shows
    one hit per chunk, the best ranked."""
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search as ks
    from server.knowledge.app import search

    class _Embedder:
        model_name = "m"

        async def embed(self, text: str) -> list[float]:
            return [1.0]

    hits = [
        ChunkHit("n", "a.md", 0, "newer", [], distance=0.1, revision=2, doc_version=20),
        ChunkHit("n", "a.md", 0, "older", [], distance=0.2, revision=1, doc_version=10),
    ]

    async def legs(q: Any) -> list[ChunkHit]:
        return hits[: q.top_k]

    async def first(input: Any, namespaces: list[str], run: Any) -> Any:
        return await run(namespaces)

    async def named(input: Any) -> list[str]:
        return ["n"]

    async def heads(org: str, sources: Any) -> dict[Any, Serving]:
        return {}

    monkeypatch.setattr(ks, "get_embedder", _Embedder)
    monkeypatch.setattr(search, "hybrid_search", legs)
    monkeypatch.setattr(ks, "_search_private_first", first)
    monkeypatch.setattr(ks, "_named_namespaces", named)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", heads)
    out = await ks.handle_knowledge_search(
        KnowledgeSearchInput(
            org="o", namespace="n", namespaces=["n"], query="text", search_mode="semantic"
        )
    )
    assert [r.content for r in out.results] == ["newer"] and out.stale_dropped == 0


@pytest.mark.asyncio
async def test_an_outline_and_a_documents_chunks_show_the_heads_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeOutlineInput
    from server.knowledge.app import document_reads
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.chunks import ChunkRow

    def row(idx: int, text: str, revision: int, version: int) -> ChunkRow:
        return ChunkRow(
            org="o",
            namespace="n",
            source_uri="a.md",
            chunk_idx=idx,
            content=text,
            chunk_metadata=f'{{"section_path": ["S"], "revision": {revision}}}',
            ingested_at=datetime(2026, 1, 1),
            embedding_model="m",
            created_at=datetime(2026, 1, 1),
            doc_version=version,
        )

    rows = [row(0, "old", 2, 20), row(0, "new", 3, 30), row(1, "pending", 4, 40)]

    async def listed(*a: Any, **kw: Any) -> list[ChunkRow]:
        return rows

    async def doc(*a: Any, **kw: Any) -> Any:
        return documents_repo.DocumentRow(
            org="o",
            namespace="n",
            source_uri="a.md",
            doc_id="d",
            content="new",
            version=31,
            revision=3,
            ingested_at=datetime(2026, 1, 1),
        )

    async def heads(org: str, sources: Any) -> dict[Any, Serving]:
        return {("n", "a.md"): _HEAD}

    class _Embedder:
        model_name = "m"

    monkeypatch.setattr(document_reads, "get_embedder", _Embedder)
    monkeypatch.setattr(knowledge_repo, "list_for_doc", listed)
    monkeypatch.setattr(documents_repo, "get_one", doc)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", heads)
    out = await document_reads.handle_knowledge_outline(
        KnowledgeOutlineInput(org="o", namespace="n", source_uri="a.md", section_path=["S"])
    )
    assert out.chunk_count == 1 and out.section_content == "new"
    chunks = await document_reads.document_chunks("o", "n", "a.md")
    assert [c.text for c in chunks] == ["new"]


@pytest.mark.asyncio
async def test_a_memory_stack_serves_only_the_heads_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`memory.stack` reads chunks itself: under the Postgres authority its knowledge
    section takes the same check and refill as a search."""
    from server.knowledge.app import search
    from server.memory.api.models_lifecycle import MemoryStackInput
    from server.memory.app import stack

    class _Embedder:
        model_name = "m"

        async def embed(self, _text: str) -> list[float]:
            return [0.0]

    ranked = [
        ChunkHit("ns", "a.md", 0, "old text", [], distance=0.1, revision=2, doc_version=20),
        ChunkHit("ns", "a.md", 0, "new text", [], distance=0.2, revision=3, doc_version=30),
        ChunkHit("ns", "b.md", 0, "other", [], distance=0.3, revision=1, doc_version=1),
    ]
    asked: list[int] = []

    async def legs(q: Any) -> list[ChunkHit]:
        asked.append(q.top_k)
        return ranked[: q.top_k]

    async def heads(org: str, sources: Any) -> dict[Any, Serving]:
        return {("ns", "a.md"): _HEAD}

    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Embedder())
    monkeypatch.setattr(search, "hybrid_search", legs)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", heads)
    fill = stack._StackFill(budget=10_000)
    await stack._stack_knowledge(
        MemoryStackInput(
            org="o", namespace="ns", agent="a", include_knowledge=True, knowledge_top_k=2
        ),
        fill,
        "text",
    )
    assert [e.value for e in fill.entries] == ["new text", "other"]
    assert asked[:2] == [2, 4]  # the page of two held a stale hit: fetched again, deeper


@pytest.mark.asyncio
async def test_a_read_that_cannot_reach_the_heads_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres unreachable: a search and an outline answer 503 POSTGRES_UNAVAILABLE
    rather than serve chunks they cannot check."""
    from server.knowledge.api.models_knowledge import KnowledgeOutlineInput, KnowledgeSearchInput
    from server.knowledge.app import document_reads, search
    from server.knowledge.app import knowledge_search as ks
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.chunks import ChunkRow

    class _Embedder:
        model_name = "m"

        async def embed(self, text: str) -> list[float]:
            return [1.0]

    async def legs(q: Any) -> list[ChunkHit]:
        return [_hit("n", "a.md", 3, 30)]

    async def first(input: Any, namespaces: list[str], run: Any) -> Any:
        return await run(namespaces)

    async def named(input: Any) -> list[str]:
        return ["n"]

    async def down(org: str, sources: Any) -> dict[Any, Serving]:
        raise pg.PostgresUnavailableError("connection refused")

    async def listed(*a: Any, **kw: Any) -> list[ChunkRow]:
        return [
            ChunkRow(
                org="o",
                namespace="n",
                source_uri="a.md",
                chunk_idx=0,
                content="text",
                ingested_at=datetime(2026, 1, 1),
                embedding_model="m",
                created_at=datetime(2026, 1, 1),
                doc_version=30,
            )
        ]

    async def doc(*a: Any, **kw: Any) -> Any:
        return documents_repo.DocumentRow(
            org="o",
            namespace="n",
            source_uri="a.md",
            doc_id="d",
            content="text",
            version=30,
            revision=3,
            ingested_at=datetime(2026, 1, 1),
        )

    monkeypatch.setattr(ks, "get_embedder", _Embedder)
    monkeypatch.setattr(document_reads, "get_embedder", _Embedder)
    monkeypatch.setattr(search, "hybrid_search", legs)
    monkeypatch.setattr(ks, "_search_private_first", first)
    monkeypatch.setattr(ks, "_named_namespaces", named)
    monkeypatch.setattr(knowledge_repo, "list_for_doc", listed)
    monkeypatch.setattr(documents_repo, "get_one", doc)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", down)
    with pytest.raises(pg.PostgresUnavailableError) as searched:
        await ks.handle_knowledge_search(
            KnowledgeSearchInput(
                org="o", namespace="n", namespaces=["n"], query="text", search_mode="semantic"
            )
        )
    assert (searched.value.code, searched.value.status_code) == ("POSTGRES_UNAVAILABLE", 503)
    with pytest.raises(pg.PostgresUnavailableError):
        await document_reads.handle_knowledge_outline(
            KnowledgeOutlineInput(org="o", namespace="n", source_uri="a.md")
        )


@pytest.mark.asyncio
async def test_the_chunks_synthesize_reads_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`document_chunks`, which knowledge.synthesize and `loom decisions extract` read,
    raises the heads read's 503 rather than return chunks it could not check."""
    from server.knowledge.app import document_reads
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store.chunks import ChunkRow

    class _Embedder:
        model_name = "m"

    async def listed(*a: Any, **kw: Any) -> list[ChunkRow]:
        return [
            ChunkRow(
                org="o",
                namespace="n",
                source_uri="a.md",
                chunk_idx=0,
                content="text",
                ingested_at=datetime(2026, 1, 1),
                embedding_model="m",
                created_at=datetime(2026, 1, 1),
                doc_version=30,
            )
        ]

    async def down(org: str, sources: Any) -> dict[Any, Serving]:
        raise pg.PostgresUnavailableError("connection refused")

    monkeypatch.setattr(document_reads, "get_embedder", _Embedder)
    monkeypatch.setattr(knowledge_repo, "list_for_doc", listed)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "serving", down)
    with pytest.raises(pg.PostgresUnavailableError):
        await document_reads.document_chunks("o", "n", "a.md")
