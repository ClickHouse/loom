"""knowledge.search hybrid retrieval service.

Vector + tag-filter + recency search over `loom.chunks`, returning top-K
results to the caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from server.core import bm25
from server.core.repo_base import _default_client
from server.core.vectors.search_settings import vector_search_settings
from server.knowledge.domain.live_chunks import live_chunk_sql


class _CHClient(Protocol):
    async def query_all(
        self,
        sql: str,
        params: dict[str, Any] | None = ...,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class HybridSearchQuery:
    org: str
    namespace: str
    embedding_model: str
    query_vec: list[float]
    top_k: int
    tags: list[str] = field(default_factory=list)
    since_seconds: int | None = None
    # Directory scope: keep chunks whose source_uri starts with any of these prefixes.
    # `source_uri` is the sort-key column after embedding_model, so each prefix is a
    # primary-key range, not a scan.
    uri_prefixes: list[str] = field(default_factory=list)
    # Lexical leg inputs; empty tokens = no lexical leg.
    tokens: list[str] = field(default_factory=list)
    idfs: list[float] = field(default_factory=list)
    avgdl: float = 0.0


@dataclass(frozen=True)
class ChunkHit:
    source_uri: str
    chunk_idx: int
    content: str
    tags: list[str]
    distance: float
    # Heading breadcrumb from chunk_metadata; () when the document had no headings.
    section_path: tuple[str, ...] = ()
    # Lexical leg: raw BM25 over the query tokens, and the fused ranking score
    # (reciprocal rank of both legs). In semantic mode `score` is the similarity; in
    # lexical mode the BM25 value.
    lexical_score: float = 0.0
    score: float = 0.0

    @property
    def section(self) -> str:
        return " > ".join(self.section_path)


RRF_K = 60.0


def fuse_rrf(
    semantic: list[ChunkHit],
    lexical: list[ChunkHit],
    *,
    top_k: int,
    w_lex: float = 1.0,
) -> list[ChunkHit]:
    """Reciprocal-rank fusion of the legs: score = 1/(k + rank_sem) +
    w_lex/(k + rank_lex), k = 60, over the union of candidates. A hit present in
    one leg only takes that leg's term. Ties break on distance, then on the chunk
    key, so the order is a function of the inputs."""
    by_key: dict[tuple[str, int], ChunkHit] = {}
    for h in [*semantic, *lexical]:
        key = (h.source_uri, h.chunk_idx)
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = h
        else:
            # Merge: keep the smaller distance and the larger leg scores.
            by_key[key] = ChunkHit(
                source_uri=h.source_uri,
                chunk_idx=h.chunk_idx,
                content=h.content,
                tags=h.tags,
                distance=min(prev.distance, h.distance),
                section_path=prev.section_path or h.section_path,
                lexical_score=max(prev.lexical_score, h.lexical_score),
            )
    sem_rank = {(h.source_uri, h.chunk_idx): r + 1 for r, h in enumerate(semantic)}
    lex_rank = {(h.source_uri, h.chunk_idx): r + 1 for r, h in enumerate(lexical)}
    fused: list[ChunkHit] = []
    for key, h in by_key.items():
        score = 0.0
        if key in sem_rank:
            score += 1.0 / (RRF_K + sem_rank[key])
        if key in lex_rank and h.lexical_score > 0:
            score += w_lex / (RRF_K + lex_rank[key])
        fused.append(
            ChunkHit(
                source_uri=h.source_uri,
                chunk_idx=h.chunk_idx,
                content=h.content,
                tags=h.tags,
                distance=h.distance,
                section_path=h.section_path,
                lexical_score=h.lexical_score,
                score=score,
            )
        )
    fused.sort(key=lambda h: (-h.score, h.distance, h.source_uri, h.chunk_idx))
    return fused[:top_k]


def _section_path_of(chunk_metadata: object) -> tuple[str, ...]:
    if not chunk_metadata or not isinstance(chunk_metadata, str):
        return ()
    try:
        meta = json.loads(chunk_metadata)
    except (TypeError, ValueError):
        return ()
    path = meta.get("section_path") if isinstance(meta, dict) else None
    if not isinstance(path, list):
        return ()
    return tuple(str(p) for p in path if str(p))


async def hybrid_search(
    q: HybridSearchQuery,
    *,
    ch: _CHClient | None = None,
) -> list[ChunkHit]:
    """One vector-ranked scan over loom.chunks with optional tag + recency
    filters. Returns top-K by cosineDistance ascending.
    """
    ch = ch or _default_client()
    where, params = _common_where(q)

    sql = f"""
        SELECT
            source_uri,
            chunk_idx,
            content,
            tags,
            chunk_metadata,
            cosineDistance(embedding, {{q:Array(Float32)}}) AS distance
        FROM loom.chunks
        WHERE {" AND ".join(where)}
        ORDER BY distance ASC
        LIMIT {{top_k:UInt32}}
        {vector_search_settings(table="loom.chunks")}
    """
    rows = await ch.query_all(sql, params=params)
    return [
        ChunkHit(
            source_uri=r["source_uri"],
            chunk_idx=int(r["chunk_idx"]),
            content=r["content"],
            tags=list(r["tags"] or []),
            distance=float(r["distance"]),
            section_path=_section_path_of(r.get("chunk_metadata")),
            score=1.0 - float(r["distance"]),
        )
        for r in rows
    ]


def _common_where(q: HybridSearchQuery) -> tuple[list[str], dict[str, Any]]:
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "embedding_model = {model:String}",
        "embedding_dim > 0",
        live_chunk_sql(),
    ]
    params: dict[str, Any] = {
        "org": q.org,
        "ns": q.namespace,
        "model": q.embedding_model,
        "q": q.query_vec,
        "top_k": int(q.top_k),
    }
    if q.tags:
        where.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = q.tags
    if q.since_seconds is not None:
        cutoff = datetime.now(UTC) - timedelta(seconds=q.since_seconds)
        where.append("ingested_at >= {since:DateTime}")
        params["since"] = cutoff
    prefixes = [p for p in (q.uri_prefixes or []) if p]
    if prefixes:
        # One startsWith per prefix, OR-ed: the analyzer turns each into a
        # range over the (…, embedding_model, source_uri) sort key.
        clauses = []
        for i, p in enumerate(prefixes):
            clauses.append(f"startsWith(source_uri, {{pfx{i}:String}})")
            params[f"pfx{i}"] = p
        where.append("(" + " OR ".join(clauses) + ")")
    return where, params


async def lexical_search(
    q: HybridSearchQuery,
    *,
    ch: _CHClient | None = None,
) -> list[ChunkHit]:
    """The lexical leg: Okapi BM25 over the query tokens (`server.core.bm25`;
    tf and the document length both counted in the chunk's words), restricted by the
    text index to chunks that contain at least one token, top-K by BM25 descending.
    Each hit also carries its cosine distance so fusion has both legs for every
    candidate. Returns [] when the query has no tokens or the corpus statistics are
    cold (no avgdl yet)."""
    if not q.tokens or not q.idfs or len(q.idfs) != len(q.tokens) or q.avgdl <= 0:
        return []
    ch = ch or _default_client()
    where, params = _common_where(q)
    params["toks"] = q.tokens
    params["idfs"] = [float(x) for x in q.idfs]
    params["avgdl"] = float(q.avgdl)
    # Candidate predicate over the lower-cased materialised copy: the text
    # index on content_lc is tokenised with splitByNonAlpha,
    # the same tokeniser the term-frequency view uses, so this is a
    # skip-index lookup, not a scan. tf is counted over the same tokens, so
    # numerator and document frequency agree at word boundaries.
    where.append("hasAnyTokens(content_lc, {toks:Array(String)})")
    # The row is split once; tf and dl both read the one array.
    bm25_expr = bm25.bm25_sum_sql(
        bm25.tf_array_sql("countEqual(words, tok)", "{toks:Array(String)}"),
        "{idfs:Array(Float32)}",
        dl="length(words)",
        avgdl="{avgdl:Float32}",
    )
    sql = f"""
        WITH splitByNonAlpha(content_lc) AS words
        SELECT
            source_uri,
            chunk_idx,
            content,
            tags,
            chunk_metadata,
            cosineDistance(embedding, {{q:Array(Float32)}}) AS distance,
            {bm25_expr} AS lexical
        FROM loom.chunks
        WHERE {" AND ".join(where)}
        ORDER BY lexical DESC, distance ASC
        LIMIT {{top_k:UInt32}}
    """
    rows = await ch.query_all(sql, params=params)
    return [
        ChunkHit(
            source_uri=r["source_uri"],
            chunk_idx=int(r["chunk_idx"]),
            content=r["content"],
            tags=list(r["tags"] or []),
            distance=float(r["distance"]),
            section_path=_section_path_of(r.get("chunk_metadata")),
            lexical_score=float(r["lexical"] or 0.0),
            score=float(r["lexical"] or 0.0),
        )
        for r in rows
    ]
