"""loom rechunk / loom lexical-stats-rebuild: migrate existing corpora onto
section chunking and keep BM25 document frequency honest."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest


def test_rechunk_reupserts_every_live_document_from_stored_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import ops
    from server.knowledge.store import documents as documents_repo

    pages = [
        [
            {
                "source_uri": "kb://a",
                "ingested_at": datetime(2026, 1, 2, tzinfo=UTC),
                "chunk_count": 3,
            },
            {
                "source_uri": "kb://b",
                "ingested_at": datetime(2026, 1, 1, tzinfo=UTC),
                "chunk_count": 2,
            },
        ],
    ]
    seen_cursors: list[Any] = []

    async def _list(
        org: str, ns: str, *, include_superseded: bool, cursor: Any, limit: int, **kw: Any
    ) -> list[Any]:
        seen_cursors.append(cursor)
        return pages.pop(0) if pages else []

    async def _get_one(org: str, ns: str, uri: str, **kw: Any) -> Any:
        if uri == "kb://b":
            return None  # vanished between list and read
        return NS(
            content="# H\n\nbody",
            title="A",
            description="d",
            tags=["t"],
            metadata='{"k": 1}',
            version=7,
        )

    upserts: list[Any] = []

    async def _upsert(inp: Any, *, read_version: int | None = None) -> Any:
        upserts.append(inp)
        assert read_version == 7  # a write since the read is refused, never undone
        return NS(chunks_created=5, chunks_superseded=3)

    monkeypatch.setattr(documents_repo, "list_with_chunk_counts", _list)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr("server.knowledge.app.writes.handle_knowledge_upsert", _upsert)

    out = asyncio.run(ops.rechunk_namespace("o", "ns", page_size=100))
    assert out["documents"] == 2 and out["rechunked"] == 1
    assert out["chunks_before"] == 5 and out["chunks_after"] == 5
    assert out["failed"] == ["kb://b: no live document row"]
    assert seen_cursors == [None]  # one short page -> no second fetch
    u = upserts[0]
    assert (u.source_uri, u.content, u.title, u.description, u.tags, u.metadata) == (
        "kb://a",
        "# H\n\nbody",
        "A",
        "d",
        ["t"],
        {"k": 1},
    )

    # dry run lists without writing; source filter narrows
    pages.append(
        [
            {
                "source_uri": "kb://a",
                "ingested_at": datetime(2026, 1, 2, tzinfo=UTC),
                "chunk_count": 3,
            }
        ]
    )
    out = asyncio.run(ops.rechunk_namespace("o", "ns", dry_run=True, source_uri="kb://a"))
    assert out["dry_run"] and out["documents"] == 1 and out["rechunked"] == 0 and len(upserts) == 1


class _FakeCH:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        table = " ".join(sql.split()).split("FROM ")[1].split(" ")[0]
        return {"n": self.counts.get(table, 0)}

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.executed.append((" ".join(sql.split()), dict(params or {})))


def test_lexical_stats_rebuild_deletes_then_reinserts_from_live_rows_per_plane() -> None:
    from server.memory.store.term_freq import rebuild

    ch = _FakeCH({"loom.chunk_term_freq": 40, "loom.memory_term_freq": 0})
    deleted = asyncio.run(rebuild(ch, org="o", namespace="ns"))
    assert deleted == {"knowledge": 40, "memory": 0}
    sqls = [s for s, _ in ch.executed]
    # knowledge: delete (had rows) then insert; memory: insert only (nothing to delete)
    assert sqls[0].startswith(
        "ALTER TABLE loom.chunk_term_freq DELETE WHERE org = {org:String} AND namespace = {ns:String}"
    )
    assert "mutations_sync = 2" in sqls[0]
    assert sqls[1].startswith("INSERT INTO loom.chunk_term_freq")
    assert (
        "uniqExactState((source_uri, chunk_idx))" in sqls[1]
        and "doc_superseded_at IS NULL" in sqls[1]
    )
    assert sqls[2].startswith("INSERT INTO loom.memory_term_freq")
    assert "tombstoned_at IS NULL" in sqls[2] and "splitByNonAlpha(content_excerpt)" in sqls[2]
    # memory_embeddings is a ReplacingMergeTree: without FINAL an older live
    # version of a tombstoned row still counts toward document frequency.
    assert "FROM loom.memory_embeddings FINAL ARRAY JOIN" in sqls[2]
    assert all(p == {"org": "o", "ns": "ns"} for _, p in ch.executed)
    # plane selection
    ch = _FakeCH({})
    asyncio.run(rebuild(ch, org="o", namespace="ns", planes=("memory",)))
    assert [s.split(" ")[2] for s, _ in ch.executed] == ["loom.memory_term_freq"]


def test_rechunk_counts_a_committed_write_awaiting_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUBLICATION_PENDING says the document committed: rechunked, not failed."""
    from server.cli import ops
    from server.core.pg_lease import PublicationPending
    from server.knowledge.store import documents as documents_repo

    pages = [[{"source_uri": "kb://a", "ingested_at": datetime(2026, 1, 2, tzinfo=UTC)}]]

    async def _list(org: str, ns: str, **kw: Any) -> list[Any]:
        return pages.pop(0) if pages else []

    async def _get_one(org: str, ns: str, uri: str, **kw: Any) -> Any:
        return NS(content="body", title="A", description="", tags=[], metadata="", version=1)

    async def _pending(inp: Any, **kw: Any) -> Any:
        raise PublicationPending()

    monkeypatch.setattr(documents_repo, "list_with_chunk_counts", _list)
    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr("server.knowledge.app.writes.handle_knowledge_upsert", _pending)
    out = asyncio.run(ops.rechunk_namespace("o", "ns", page_size=100))
    assert (out["rechunked"], out["failed"]) == (1, [])
