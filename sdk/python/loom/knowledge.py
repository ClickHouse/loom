"""Knowledge client. Surface: add / search / upsert / get / list / delete +
bulk_add / bulk_delete (batch ops) + dedup_list / dedup_resolve (admin)."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


@dataclass(frozen=True)
class SearchHit:
    source_uri: str
    chunk_idx: int
    content: str
    tags: list[str]
    distance: float
    similarity: float
    # The namespace the chunk came from (a namespace_scope="all_mine" fan-out
    # reads many) and its heading breadcrumb; empty for unstructured text.
    namespace: str = ""
    section_path: tuple[str, ...] = ()
    section: str = ""


def _hit(payload: dict[str, Any]) -> SearchHit:
    return SearchHit(
        source_uri=payload["source_uri"],
        chunk_idx=int(payload["chunk_idx"]),
        content=payload["content"],
        tags=list(payload["tags"]),
        distance=float(payload["distance"]),
        similarity=float(payload["similarity"]),
        namespace=str(payload.get("namespace") or ""),
        section_path=tuple(str(x) for x in (payload.get("section_path") or ())),
        section=str(payload.get("section") or ""),
    )


class KnowledgeClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def _scope(self) -> dict[str, Any]:
        return {"org": self._agent.org, "namespace": self._agent.namespace}

    async def add(
        self,
        content: str,
        *,
        source_uri: str,
        title: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = self._scope()
        payload.update(
            {
                "content": content,
                "source_uri": source_uri,
                "title": title,
                "tags": tags or [],
                "metadata": metadata or {},
            }
        )
        return await self._agent.transport.post("/v1/knowledge.add", payload)

    async def dedup_list(
        self,
        *,
        limit: int = 50,
        include_chunk_clusters: bool = False,
    ) -> dict[str, Any]:
        """List cross-URI doc duplicate clusters in this namespace
        (ADMIN). Set include_chunk_clusters=True
        for the higher-cardinality chunk-overlap surface."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "limit": limit,
            "include_chunk_clusters": include_chunk_clusters,
        }
        return await self._agent.transport.post("/v1/knowledge.dedup_list", payload)

    async def dedup_resolve(
        self,
        canonical_uri: str,
        duplicate_uri: str,
        *,
        decision: str,
        confirm_duplicate_uri: str,
    ) -> dict[str, Any]:
        """Soft-supersede one side of a doc duplicate pair (ADMIN).
        `keep_canonical` supersedes the duplicate URI; `swap`
        supersedes the canonical. `confirm_duplicate_uri` defense."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "canonical_uri": canonical_uri,
            "duplicate_uri": duplicate_uri,
            "decision": decision,
            "confirm_duplicate_uri": confirm_duplicate_uri,
        }
        return await self._agent.transport.post("/v1/knowledge.dedup_resolve", payload)

    async def upsert(
        self,
        content: str,
        *,
        source_uri: str,
        title: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert (or replace) content for a source_uri. Prior chunks are
        soft-superseded; search returns the latest content by default."""
        payload: dict[str, Any] = self._scope()
        payload.update(
            {
                "content": content,
                "source_uri": source_uri,
                "title": title,
                "tags": tags or [],
                "metadata": metadata or {},
            }
        )
        return await self._agent.transport.post("/v1/knowledge.upsert", payload)

    async def bulk_delete(
        self,
        source_uris: list[str],
    ) -> dict[str, Any]:
        """Soft-delete many documents in one call.

        Returns {"deleted": [{source_uri, chunks_tombstoned}, ...],
                 "total_chunks_tombstoned": N}.
        Idempotent — re-running on already-deleted source_uris returns 0s."""
        payload = self._scope()
        payload["source_uris"] = list(source_uris)
        return await self._agent.transport.post(
            "/v1/knowledge.bulk_delete",
            payload,
        )

    async def bulk_add(
        self,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Batched ingestion. Each entry is a dict with at least
        `content` and `source_uri`. Optional: `title`, `tags`, `metadata`.
        Returns {"written": [{source_uri, doc_id, chunks_created}, ...]}."""
        payload = self._scope()
        payload["entries"] = entries
        return await self._agent.transport.post(
            "/v1/knowledge.bulk_add",
            payload,
        )

    async def get(
        self,
        source_uri: str,
        *,
        include_superseded: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch a single document by source_uri. Returns None when missing."""
        payload = self._scope()
        payload["source_uri"] = source_uri
        payload["include_superseded"] = include_superseded
        result = await self._agent.transport.post("/v1/knowledge.get", payload)
        if not result.get("found"):
            return None
        return result

    async def synthesize(
        self,
        source_uris: list[str] | None,
        task: str,
        *,
        datasets: list[dict[str, Any]] | None = None,
        window_chars: int = 24000,
        max_answer_chars: int = 8000,
        max_windows: int = 64,
    ) -> dict[str, Any]:
        """Complete `task` over whole documents by map-reduce and, with `datasets`
        (each `{"source_uri", "dataset_id", "alias"}`, as knowledge.query takes them),
        a calculation over every row of them; `source_uris` may be None when datasets
        are named. Returns `{"answer", "windows", "chunks_read", "documents",
        "reduce_rounds", "citations", "cited_chunks", "unsupported_citations",
        "truncated", "abstain", "abstain_reason", "clarification", "calculations",
        "model"}`."""
        return await self._agent.transport.post(
            "/v1/knowledge.synthesize",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "source_uris": list(source_uris or []),
                "datasets": list(datasets or []),
                "task": task,
                "window_chars": window_chars,
                "max_answer_chars": max_answer_chars,
                "max_windows": max_windows,
            },
        )

    async def outline(
        self,
        source_uri: str,
        *,
        section_path: list[str] | None = None,
        include_children: bool = True,
    ) -> dict[str, Any]:
        """A document's table of contents from its chunks' section metadata,
        and, when `section_path` is given, that section's assembled text."""
        payload: dict[str, Any] = {
            **self._scope(),
            "source_uri": source_uri,
            "section_path": list(section_path or []),
            "include_children": include_children,
        }
        return await self._agent.transport.post("/v1/knowledge.outline", payload)

    async def download(self, source_uri: str) -> bytes:
        """The document as uploaded, or its stored text as Markdown when no
        original file was kept."""
        resp = await self._agent.transport.request(
            "GET", "/v1/knowledge.download", params={**self._scope(), "source_uri": source_uri}
        )
        return resp.content

    async def list(
        self,
        *,
        tags: list[str] | None = None,
        tags_every: bool = False,
        include_superseded: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Paginated listing of documents in this namespace.

        Returns {"rows": [...], "next_cursor": str | None}. Walk pages by
        threading next_cursor back in. `tags` matches any one of them, in any
        case; `tags_every=True` needs every one."""
        payload = self._scope()
        payload.update(
            {
                "tags": tags or [],
                "include_superseded": include_superseded,
                "limit": limit,
            }
        )
        if tags_every:
            payload["tags_every"] = True
        if cursor is not None:
            payload["cursor"] = cursor
        return await self._agent.transport.post("/v1/knowledge.list", payload)

    async def delete(
        self,
        source_uri: str,
    ) -> dict[str, Any]:
        """Soft-delete a document by source_uri.

        Tombstones live chunks; searches stop returning them immediately.
        Idempotent — calling on an already-deleted document is a no-op
        and returns `deleted=False, chunks_tombstoned=0`."""
        payload = self._scope()
        payload["source_uri"] = source_uri
        return await self._agent.transport.post("/v1/knowledge.delete", payload)

    async def search(
        self,
        query: str,
        *,
        tags: builtins.list[str] | None = None,
        since_seconds: int | None = None,
        top_k: int = 5,
        uri_prefix: builtins.list[str] | None = None,
        **extra: Any,
    ) -> builtins.list[SearchHit]:
        """`uri_prefix` scopes the search to documents whose
        source_uri starts with one of the prefixes (a
        primary-key range, not a post-filter). `**extra` rides the body as-is:
        any field the server accepts is reachable without an
        SDK release; `loom.ops.KnowledgeSearchInput` names them."""
        payload: dict[str, Any] = self._scope()
        payload.update({"query": query, "tags": tags or [], "top_k": top_k})
        if since_seconds is not None:
            payload["since_seconds"] = since_seconds
        if uri_prefix:
            payload["uri_prefix"] = list(uri_prefix)
        payload.update(extra)
        result = await self._agent.transport.post("/v1/knowledge.search", payload)
        return [_hit(r) for r in result.get("results", [])]
