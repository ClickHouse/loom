"""knowledge.dedup_list / dedup_resolve: cross-URI duplicate documents and chunks."""

from __future__ import annotations

from datetime import UTC, datetime

from server.knowledge.api.models_knowledge import (
    KnowledgeDedupListInput,
    KnowledgeDedupListOutput,
    KnowledgeDedupResolveInput,
    KnowledgeDedupResolveOutput,
    _KnowledgeChunkCluster,
    _KnowledgeDocCluster,
)


async def handle_knowledge_dedup_list(
    input: KnowledgeDedupListInput,
) -> KnowledgeDedupListOutput:
    """List cross-URI doc + optional chunk duplicate clusters."""
    from server.knowledge.store import chunks as knowledge_repo

    doc_rows = await knowledge_repo.find_doc_duplicate_clusters(
        input.org,
        input.namespace,
        limit=input.limit,
    )
    doc_clusters = [
        _KnowledgeDocCluster(
            content_sha256=r.get("content_sha256", "") or "",
            doc_count=int(r.get("doc_count") or 0),
            docs=[
                {
                    "doc_id": str(d[0]),
                    "source_uri": d[1],
                    "title": d[2],
                    "ingested_at": d[3],
                }
                for d in (r.get("docs") or [])
            ],
        )
        for r in doc_rows
    ]

    chunk_clusters: list[_KnowledgeChunkCluster] = []
    if input.include_chunk_clusters:
        chunk_rows = await knowledge_repo.find_chunk_duplicate_clusters(
            input.org,
            input.namespace,
            limit=input.limit,
        )
        chunk_clusters = [
            _KnowledgeChunkCluster(
                content_sha256=r.get("content_sha256", "") or "",
                chunk_count=int(r.get("chunk_count") or 0),
                uri_count=int(r.get("uri_count") or 0),
                samples=[
                    {"source_uri": s[0], "chunk_idx": int(s[1])} for s in (r.get("samples") or [])
                ],
            )
            for r in chunk_rows
        ]

    return KnowledgeDedupListOutput(
        doc_clusters=doc_clusters,
        chunk_clusters=chunk_clusters,
    )


async def handle_knowledge_dedup_resolve(
    input: KnowledgeDedupResolveInput,
) -> KnowledgeDedupResolveOutput:
    """Soft-supersede one side of a document duplicate pair.

    `keep_canonical` supersedes the duplicate URI; `swap` supersedes
    the canonical instead.
    """
    from fastapi import HTTPException

    from server.knowledge.store import chunks as knowledge_repo

    if input.confirm_duplicate_uri != input.duplicate_uri:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONFIRM_DUPLICATE_URI_MISMATCH",
                "message": (
                    "`confirm_duplicate_uri` must equal `duplicate_uri`. "
                    "Mid-weight defense against destructive UI-dropdown "
                    "accidents."
                ),
            },
        )

    target_uri = input.duplicate_uri if input.decision == "keep_canonical" else input.canonical_uri
    now = datetime.now(UTC).replace(tzinfo=None)
    await knowledge_repo.mark_doc_superseded(
        input.org,
        input.namespace,
        target_uri,
        when=now,
    )

    return KnowledgeDedupResolveOutput(
        canonical_uri=input.canonical_uri,
        duplicate_uri=input.duplicate_uri,
        decision=input.decision,
        superseded_uri=target_uri,
    )
