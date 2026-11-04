"""Repository for the loom.chunks table family.

Knowledge ingestion writes one row per chunk into `loom.chunks`. Each
chunk inherits identity from its parent document (source_uri,
embedding_model). This module exposes the byte-level chunk operations;
the vector-search SQL lives in the search service.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, TypedDict, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from server.core.hashing import sha256_document_text
from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.chunking import Chunk, embedding_text
from server.knowledge.domain.extract.structural import StructuralChunk
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL, SUPERSESSIONS_TABLE


class ChunkRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    org: str
    namespace: str
    source_uri: str
    chunk_idx: int
    content: str
    tags: list[str] = []
    doc_metadata: str = "{}"
    chunk_metadata: str = "{}"
    ingested_at: datetime
    doc_superseded_at: datetime | None = None
    embedding_model: str
    embedding_dim: int = 0
    embedding: list[float] = []
    created_at: datetime
    embed_attempts: int = 0
    embed_last_error: str = ""
    embed_failed_at: datetime | None = None
    doc_version: int = 0


class ChunkInsertRow(TypedDict):
    """One loom.chunks row as the ingest paths write it (`build_chunk_rows`)."""

    org: str
    namespace: str
    source_uri: str
    chunk_idx: int
    content: str
    context: str
    tags: list[str]
    doc_metadata: str
    chunk_metadata: str
    ingested_at: datetime
    doc_superseded_at: datetime | None
    doc_version: int
    embedding_model: str
    embedding_dim: int
    embedding: list[float]
    created_at: datetime
    content_sha256: str
    agent: str
    user_id: str
    session_id: str
    source_class: str
    keys: list[str]
    embed_input_sha256: str


def build_chunk_rows(
    *,
    org: str,
    namespace: str,
    source_uri: str,
    chunks: Sequence[Chunk | StructuralChunk],
    contexts: Sequence[str],
    embeddings: Sequence[list[float]],
    tags: list[str],
    metadata: str,
    doc_version: int,
    revision: int,
    doc_format: str,
    embedding_model: str,
    embedding_dim: int,
    now: datetime,
    author: Mapping[str, str],
) -> list[ChunkInsertRow]:
    """One ingestion's loom.chunks rows, every column set: each chunk with its
    context (`chunk_context`) and vector, the sha256 of the text the vector was
    computed from (`embedding_text`), the document's tags and metadata (JSON),
    its own section metadata with the document revision and format it is written
    as (what a search hit and a citation name), exact keys and content hash (what
    upsert's duplicate count and the dedup report match on), and the document's
    provenance (`author`: agent, user_id, session_id, source_class) that search
    returns and filters on."""
    return [
        {
            "org": org,
            "namespace": namespace,
            "source_uri": source_uri,
            "chunk_idx": chunk.idx,
            "content": chunk.text,
            "context": context,
            "tags": tags,
            "doc_metadata": metadata,
            "chunk_metadata": json.dumps(
                {**chunk.meta, "revision": revision, "format": doc_format}, sort_keys=True
            ),
            "ingested_at": now,
            "doc_superseded_at": None,
            "doc_version": doc_version,
            "embedding_model": embedding_model,
            "embedding_dim": embedding_dim,
            "embedding": embedding,
            "created_at": now,
            "content_sha256": sha256_document_text(chunk.text),
            "agent": author["agent"],
            "user_id": author["user_id"],
            "session_id": author["session_id"],
            "source_class": author["source_class"],
            "keys": list(chunk.keys),
            "embed_input_sha256": hashlib.sha256(
                embedding_text(context, chunk.text).encode()
            ).hexdigest(),
        }
        for chunk, context, embedding in zip(chunks, contexts, embeddings, strict=True)
    ]


class DocDuplicateCluster(TypedDict):
    content_sha256: str
    doc_count: int
    # (doc_id, source_uri, title, ingested_at) per document, at most 50
    docs: list[tuple[UUID, str, str, datetime]]


class ChunkDuplicateCluster(TypedDict):
    content_sha256: str
    chunk_count: int
    uri_count: int
    # (source_uri, chunk_idx) per chunk, at most 20
    samples: list[tuple[str, int]]


class DocHashMatch(TypedDict):
    source_uri: str
    doc_id: UUID
    title: str
    ingested_at: datetime


def _hydrate(row: dict[str, Any]) -> ChunkRow:
    return ChunkRow(
        org=row["org"],
        namespace=row["namespace"],
        source_uri=row["source_uri"],
        chunk_idx=int(row["chunk_idx"]),
        content=row["content"],
        tags=list(row.get("tags") or []),
        doc_metadata=row.get("doc_metadata") or "{}",
        chunk_metadata=row.get("chunk_metadata") or "{}",
        ingested_at=row["ingested_at"],
        doc_superseded_at=row.get("doc_superseded_at"),
        embedding_model=row["embedding_model"],
        embedding_dim=int(row.get("embedding_dim", 0) or 0),
        embedding=list(row.get("embedding") or []),
        created_at=row["created_at"],
        embed_attempts=int(row.get("embed_attempts", 0) or 0),
        embed_last_error=row.get("embed_last_error", "") or "",
        embed_failed_at=row.get("embed_failed_at"),
        doc_version=int(row.get("doc_version", 0) or 0),
    )


async def list_for_doc(
    org: str,
    namespace: str,
    source_uri: str,
    *,
    embedding_model: str,
    ch: _CHClient | None = None,
) -> list[ChunkRow]:
    """All active (non-superseded) chunks for one document, ordered by chunk_idx."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT org, namespace, source_uri, chunk_idx, content, tags,
               doc_metadata, chunk_metadata, ingested_at, doc_superseded_at,
               embedding_model, embedding_dim, embedding, created_at,
               embed_attempts, embed_last_error, embed_failed_at, doc_version
        FROM loom.chunks
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND embedding_model = {em:String}
          AND source_uri = {uri:String}
          AND """
        + LIVE_CHUNK_SQL
        + """
        ORDER BY chunk_idx ASC
        """,
        params={
            "org": org,
            "ns": namespace,
            "em": embedding_model,
            "uri": source_uri,
        },
    )
    return [_hydrate(r) for r in rows]


async def mark_doc_superseded(
    org: str,
    namespace: str,
    source_uri: str,
    *,
    embedding_model: str | None = None,
    before_version: int | None = None,
    only_version: int | None = None,
    when: datetime | None = None,
    ch: _CHClient | None = None,
) -> None:
    """Mark every chunk of a document as superseded, or with `before_version`
    every chunk of an older ingestion (a publish retires what it replaced), or
    with `only_version` the chunks of one ingestion (a publish that stopped
    before its document row).

    Omit `embedding_model` to supersede across every embedding model for
    this doc (the knowledge.delete wire semantics). `when` defaults to
    CH's now().
    """
    ch = ch or _default_client()
    where = ["source_uri = {uri:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace, "uri": source_uri}
    if before_version is not None:
        where.append("doc_version < {before:UInt64}")
        params["before"] = before_version
    if only_version is not None:
        where.append("doc_version = {only:UInt64}")
        params["only"] = only_version
    if embedding_model is not None:
        where.append("embedding_model = {em:String}")
        params["em"] = embedding_model
    await _record_supersessions(ch, where, params, when)


async def _record_supersessions(
    ch: _CHClient,
    where: list[str],
    params: dict[str, Any],
    when: datetime | None,
) -> None:
    """Retire every live ingestion matching `where` by listing it in
    loom.chunk_supersessions — one row per (model, uri, doc_version) —
    instead of a lightweight UPDATE of loom.chunks, which leaves a patch part
    (cleared only once the base parts it covers merge away) that turns the
    codes-first vector read off (ClickHouse issue #119885). An ingestion
    written after this statement carries a newer doc_version and stays live,
    even in the same second. `params` must carry `org` and `ns`."""
    if when is not None:
        at_sql = "{when:DateTime}"
        params = {**params, "when": when}
    else:
        at_sql = "now()"
    await ch.execute(
        f"""
        INSERT INTO {SUPERSESSIONS_TABLE}
            (org, namespace, embedding_model, source_uri, doc_version, superseded_at)
        SELECT DISTINCT org, namespace, embedding_model, source_uri, doc_version, {at_sql}
        FROM loom.chunks
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND {" AND ".join(where)}
          AND {LIVE_CHUNK_SQL}
        """,
        params=params,
    )


async def count_active_for_doc(
    org: str,
    namespace: str,
    source_uri: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Count non-superseded chunks for one document."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT count() AS n
        FROM loom.chunks
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND source_uri = {uri:String}
          AND """
        + LIVE_CHUNK_SQL
        + """
        """,
        params={"org": org, "ns": namespace, "uri": source_uri},
    )
    return int(row["n"]) if row else 0


async def count_version(
    org: str, namespace: str, source_uri: str, doc_version: int, *, ch: _CHClient | None = None
) -> int:
    """Live chunks of one ingestion of a document (`doc_version`)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT count() AS n FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String}"
        f" AND source_uri = {{uri:String}} AND doc_version = {{v:UInt64}} AND {LIVE_CHUNK_SQL}",
        params={"org": org, "ns": namespace, "uri": source_uri, "v": doc_version},
    )
    return int(row["n"]) if row else 0


async def count_active_by_uri(
    org: str,
    namespace: str,
    source_uris: list[str],
    *,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """Per-uri active chunk counts in one CH query.

    URIs with no active chunks are omitted from the result — callers
    should default-zero on miss.
    """
    if not source_uris:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT source_uri, count() AS n
        FROM loom.chunks
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND source_uri IN {uris:Array(String)}
          AND """
        + LIVE_CHUNK_SQL
        + """
        GROUP BY source_uri
        """,
        params={"org": org, "ns": namespace, "uris": source_uris},
    )
    return {r["source_uri"]: int(r["n"]) for r in rows}


async def mark_docs_superseded(
    org: str,
    namespace: str,
    source_uris: list[str],
    *,
    when: datetime | None = None,
    ch: _CHClient | None = None,
) -> None:
    """Bulk variant of `mark_doc_superseded`: retire every chunk for the
    listed URIs in one statement."""
    if not source_uris:
        return
    ch = ch or _default_client()
    await _record_supersessions(
        ch,
        ["source_uri IN {uris:Array(String)}"],
        {"org": org, "ns": namespace, "uris": source_uris},
        when,
    )


async def find_doc_duplicate_clusters(
    org: str,
    namespace: str,
    *,
    limit: int = 50,
    ch: _CHClient | None = None,
) -> list[DocDuplicateCluster]:
    """List live docs grouped by `content_sha256` where the cluster
    has more than one distinct `source_uri`. Excludes empty hashes.
    Ordered by cluster size desc.
    """
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT
            content_sha256,
            count() AS doc_count,
            groupArray(50)((doc_id, source_uri, title, ingested_at)) AS docs
        FROM loom.documents FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND superseded_at IS NULL
          AND content_sha256 != ''
        GROUP BY content_sha256
        HAVING uniqExact(source_uri) > 1
        ORDER BY doc_count DESC
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )
    return cast(list[DocDuplicateCluster], rows)


async def find_chunk_duplicate_clusters(
    org: str,
    namespace: str,
    *,
    limit: int = 50,
    ch: _CHClient | None = None,
) -> list[ChunkDuplicateCluster]:
    """List chunks grouped by `content_sha256` where the cluster spans
    more than one `source_uri` — the boilerplate-overlap signal. Higher
    cardinality than doc clustering, so callers opt in.
    """
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT
            content_sha256,
            count() AS chunk_count,
            uniqExact(source_uri) AS uri_count,
            groupArray(20)((source_uri, chunk_idx)) AS samples
        FROM loom.chunks
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND """
        + LIVE_CHUNK_SQL
        + """
          AND content_sha256 != ''
        GROUP BY content_sha256
        HAVING uri_count > 1
        ORDER BY chunk_count DESC
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )
    return cast(list[ChunkDuplicateCluster], rows)


async def find_doc_by_content_sha256(
    org: str,
    namespace: str,
    content_sha256: str,
    *,
    excluding_source_uri: str = "",
    ch: _CHClient | None = None,
) -> DocHashMatch | None:
    """Find a live document matching this content hash under a
    *different* source_uri (cross-URI duplication detection).

    Excludes the current upsert's own source_uri (no self-match) and
    empty `content_sha256` (a row without a hash must not match a non-empty
    hash). Returns first match by ingested_at DESC, or None.
    """
    if not content_sha256:
        return None
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT source_uri, doc_id, title, ingested_at
        FROM loom.documents FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND content_sha256 = {sha:String}
          AND content_sha256 != ''
          AND source_uri != {ex:String}
          AND superseded_at IS NULL
        ORDER BY ingested_at DESC
        LIMIT 1
        """,
        params={
            "org": org,
            "ns": namespace,
            "sha": content_sha256,
            "ex": excluding_source_uri,
        },
    )
    return cast(DocHashMatch | None, row)


async def count_matching_chunk_hashes(
    org: str,
    namespace: str,
    chunk_hashes: list[str],
    *,
    excluding_source_uri: str = "",
    ch: _CHClient | None = None,
) -> int:
    """Count live chunks whose `content_sha256` is in `chunk_hashes` and
    that live under a *different* source_uri than the current upsert —
    the boilerplate-overlap signal. Pure metadata; storage stays
    per-document.
    """
    non_empty = [h for h in chunk_hashes if h]
    if not non_empty:
        return 0
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT count() AS n
        FROM loom.chunks
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND content_sha256 IN {hashes:Array(String)}
          AND source_uri != {ex:String}
          AND """
        + LIVE_CHUNK_SQL
        + """
        """,
        params={
            "org": org,
            "ns": namespace,
            "hashes": non_empty,
            "ex": excluding_source_uri,
        },
    )
    return int(row["n"]) if row else 0


async def insert_chunks(
    rows: list[ChunkInsertRow],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Bulk insert. async_insert=0 so chunks are immediately visible to
    the next search.
    """
    ch = ch or _default_client()
    if not rows:
        return 0
    await ch.insert(
        "loom.chunks",
        cast(list[dict[str, Any]], rows),
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    return len(rows)


async def vectors_by_input(
    org: str,
    namespace: str,
    embedding_model: str,
    embedding_dim: int,
    input_shas: list[str],
    *,
    ch: _CHClient | None = None,
) -> dict[str, list[float]]:
    """The stored vector of each embedding input (by its sha256) this namespace
    already embedded under `embedding_model`, live or not: the same text under
    the same model is the same vector, and no namespace reuses another's."""
    if not input_shas:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT embed_input_sha256 AS sha, any(embedding) AS vector FROM loom.chunks"
        " WHERE org = {org:String} AND namespace = {ns:String} AND embedding_model = {em:String}"
        " AND embed_input_sha256 IN {shas:Array(String)} AND embedding_dim = {dim:UInt16}"
        " AND length(embedding) = {dim:UInt16}"
        " GROUP BY embed_input_sha256",
        params={
            "org": org,
            "ns": namespace,
            "em": embedding_model,
            "dim": embedding_dim,
            "shas": input_shas,
        },
    )
    return {str(r["sha"]): [float(x) for x in r["vector"]] for r in rows}
