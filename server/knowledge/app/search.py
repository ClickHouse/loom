"""knowledge.search hybrid retrieval service.

Vector + tag-filter + recency search over `loom.chunks`, returning top-K
results to the caller.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypedDict

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
    # Every namespace the statement reads, in one `namespace IN` range set.
    namespaces: tuple[str, ...]
    embedding_model: str
    query_vec: list[float]
    top_k: int
    tags: list[str] = field(default_factory=list)
    since_seconds: int | None = None
    # Directory scope: keep chunks whose source_uri starts with any of these prefixes.
    # `source_uri` is the sort-key column after embedding_model, so each prefix is a
    # primary-key range, not a scan.
    uri_prefixes: list[str] = field(default_factory=list)
    # Only chunks of documents written in these sessions / of these classes; empty = all.
    session_ids: list[str] = field(default_factory=list)
    source_classes: list[str] = field(default_factory=list)
    # Lexical leg inputs; empty tokens = no lexical leg.
    tokens: list[str] = field(default_factory=list)
    idfs: list[float] = field(default_factory=list)
    avgdl: float = 0.0


@dataclass(frozen=True)
class ChunkHit:
    namespace: str
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
    # When the chunk's document was written; None from a source without it.
    ingested_at: datetime | None = None
    # The document's provenance: the agent and session that wrote it, its class.
    agent: str = "-"
    session_id: str = "-"
    source_class: str = ""

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
    by_key: dict[tuple[str, str, int], ChunkHit] = {}
    for h in [*semantic, *lexical]:
        key = (h.namespace, h.source_uri, h.chunk_idx)
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = h
        else:
            # Merge: keep the smaller distance and the larger leg scores.
            by_key[key] = dataclasses.replace(
                h,
                distance=min(prev.distance, h.distance),
                section_path=prev.section_path or h.section_path,
                lexical_score=max(prev.lexical_score, h.lexical_score),
                ingested_at=prev.ingested_at or h.ingested_at,
            )
    sem_rank = {(h.namespace, h.source_uri, h.chunk_idx): r + 1 for r, h in enumerate(semantic)}
    lex_rank = {(h.namespace, h.source_uri, h.chunk_idx): r + 1 for r, h in enumerate(lexical)}
    fused: list[ChunkHit] = []
    for key, h in by_key.items():
        score = 0.0
        if key in sem_rank:
            score += 1.0 / (RRF_K + sem_rank[key])
        if key in lex_rank and h.lexical_score > 0:
            score += w_lex / (RRF_K + lex_rank[key])
        fused.append(dataclasses.replace(h, score=score))
    fused.sort(key=lambda h: (-h.score, h.distance, h.namespace, h.source_uri, h.chunk_idx))
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
            namespace,
            source_uri,
            chunk_idx,
            content,
            tags,
            chunk_metadata,
            ingested_at,
            agent,
            session_id,
            toString(source_class) AS source_class,
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
            namespace=r["namespace"],
            source_uri=r["source_uri"],
            chunk_idx=int(r["chunk_idx"]),
            content=r["content"],
            tags=list(r["tags"] or []),
            distance=float(r["distance"]),
            section_path=_section_path_of(r.get("chunk_metadata")),
            score=1.0 - float(r["distance"]),
            ingested_at=r.get("ingested_at"),
            **_provenance(r),
        )
        for r in rows
    ]


class _Provenance(TypedDict):
    agent: str
    session_id: str
    source_class: str


def _provenance(row: dict[str, Any]) -> _Provenance:
    return {
        "agent": str(row.get("agent") or "-"),
        "session_id": str(row.get("session_id") or "-"),
        "source_class": str(row.get("source_class") or ""),
    }


def _common_where(q: HybridSearchQuery) -> tuple[list[str], dict[str, Any]]:
    where = [
        "org = {org:String}",
        "namespace IN {nss:Array(String)}",
        "embedding_model = {model:String}",
        "embedding_dim > 0",
        live_chunk_sql(),
    ]
    params: dict[str, Any] = {
        "org": q.org,
        "nss": list(q.namespaces),
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
    if q.session_ids:
        where.append("session_id IN {sessions:Array(String)}")
        params["sessions"] = list(q.session_ids)
    if q.source_classes:
        where.append("toString(source_class) IN {classes:Array(String)}")
        params["classes"] = list(q.source_classes)
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
    tf and the document length both counted in the words of the chunk's context and
    text), restricted by the text indexes to chunks whose context or text contains at
    least one token, top-K by BM25 descending.
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
    # Both text indexes use the df views' tokeniser; a part without one of them
    # (merged, or inserted before the context index) is read in full.
    where.append(
        "(hasAnyTokens(content_lc, {toks:Array(String)})"
        " OR hasAnyTokens(context_lc, {toks:Array(String)}))"
    )
    # A chunk with no text is left out of the corpus statistics and the df views, so it
    # is no lexical candidate either, whatever its context.
    where.append("content != ''")
    # The row is split once; tf and dl both read the one array.
    bm25_expr = bm25.bm25_sum_sql(
        bm25.tf_array_sql("countEqual(words, tok)", "{toks:Array(String)}"),
        "{idfs:Array(Float32)}",
        dl="length(words)",
        avgdl="{avgdl:Float32}",
    )
    # Direct read is off (the SETTINGS line), as the memory, tracker and code-grep
    # text-index legs set it: it can drop rows under ORDER BY ... LIMIT.
    sql = f"""
        WITH arrayConcat(splitByNonAlpha(context_lc), splitByNonAlpha(content_lc)) AS words
        SELECT
            namespace,
            source_uri,
            chunk_idx,
            content,
            tags,
            chunk_metadata,
            ingested_at,
            agent,
            session_id,
            toString(source_class) AS source_class,
            cosineDistance(embedding, {{q:Array(Float32)}}) AS distance,
            {bm25_expr} AS lexical
        FROM loom.chunks
        WHERE {" AND ".join(where)}
        ORDER BY lexical DESC, distance ASC
        LIMIT {{top_k:UInt32}}
        SETTINGS query_plan_direct_read_from_text_index = 0
    """
    rows = await ch.query_all(sql, params=params)
    return [
        ChunkHit(
            namespace=r["namespace"],
            source_uri=r["source_uri"],
            chunk_idx=int(r["chunk_idx"]),
            content=r["content"],
            tags=list(r["tags"] or []),
            distance=float(r["distance"]),
            section_path=_section_path_of(r.get("chunk_metadata")),
            lexical_score=float(r["lexical"] or 0.0),
            ingested_at=r.get("ingested_at"),
            score=float(r["lexical"] or 0.0),
            **_provenance(r),
        )
        for r in rows
    ]
