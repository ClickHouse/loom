"""Knowledge client. Surface: add / search / upsert / get / list / delete +
bulk_add / bulk_delete (batch ops) + dedup_list / dedup_resolve (admin) +
upload / ingest / ingest_status / ingest_cancel / wait_for_ingest / ingest_file
(a file's bytes, extracted by the server's extractor service)."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
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
    # The section is noise: unanswered questions alone, or dated notes with no decision.
    noise: bool = False


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
        noise=bool(payload.get("noise")),
    )


# The states a knowledge.ingest job ends in; any other is still running.
INGEST_DONE = frozenset({"ready", "partial", "failed", "cancelled"})


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
        body: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "source_uris": list(source_uris or []),
            "task": task,
            "window_chars": window_chars,
            "max_answer_chars": max_answer_chars,
            "max_windows": max_windows,
        }
        if datasets:  # only when named, so an older server names nothing as ignored
            body["datasets"] = list(datasets)
        return await self._agent.transport.post("/v1/knowledge.synthesize", body)

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

    async def upload(self, data: bytes, filename: str, *, content_type: str = "") -> dict[str, Any]:
        """Store a file's bytes for `ingest`, checked against their sha256 on the
        server. The format comes from `filename` (else `content_type`). Returns
        `{"upload_id", "namespace", "sha256", "size", "filename", "format",
        "expires_at"}`."""
        params = {
            **self._scope(),
            "filename": filename,
            "content_type": content_type,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        return await self._agent.transport.request_json(
            "POST", "/v1/knowledge.upload", params=params, content=data
        )

    async def ingest(
        self,
        upload_id: str,
        *,
        source_uri: str,
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        allow_partial: bool = False,
        options: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Extract an uploaded file and publish it as the next revision of
        `source_uri`, as a job: returns `{"ingestion_id", "state", "source_uri",
        "namespace"}`; `ingest_status` or `wait_for_ingest` follow it. `options`
        holds `format`, `encoding` or `sql_dialect` when the file cannot say."""
        payload: dict[str, Any] = self._scope()
        payload.update(
            {
                "upload_id": upload_id,
                "source_uri": source_uri,
                "title": title,
                "description": description,
                "tags": tags or [],
                "metadata": metadata or {},
                "expected_revision": expected_revision,
                "allow_partial": allow_partial,
                "options": options or {},
            }
        )
        return await self._agent.transport.post("/v1/knowledge.ingest", payload)

    async def ingest_status(self, ingestion_id: str) -> dict[str, Any]:
        """A job's state, stage, coverage, failure and published revision."""
        payload = {**self._scope(), "ingestion_id": ingestion_id}
        return await self._agent.transport.post("/v1/knowledge.ingest_status", payload)

    async def ingest_cancel(self, ingestion_id: str) -> dict[str, Any]:
        """Stop a job before it publishes; a published revision stays published."""
        payload = {**self._scope(), "ingestion_id": ingestion_id}
        return await self._agent.transport.post("/v1/knowledge.ingest_cancel", payload)

    async def wait_for_ingest(
        self, ingestion_id: str, *, timeout_s: float = 600.0, poll_s: float = 1.0
    ) -> dict[str, Any]:
        """`ingest_status` once the job ends (ready, partial, failed or cancelled).
        Raises TimeoutError, the job still running, after `timeout_s`."""
        deadline = time.monotonic() + timeout_s
        while True:
            status = await self.ingest_status(ingestion_id)
            if not status.get("found") or status.get("state") in INGEST_DONE:
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"ingestion {ingestion_id} is still {status.get('state')} "
                    f"after {timeout_s:g}s; knowledge.ingest_status follows it"
                )
            await asyncio.sleep(poll_s)

    async def ingest_file(
        self,
        path: str | Path,
        *,
        source_uri: str = "",
        wait: bool = True,
        timeout_s: float = 600.0,
        **ingest_args: Any,
    ) -> dict[str, Any]:
        """Upload a local file and ingest it, named as the ingest CLI names it:
        `source_uri` defaults to its `file://` path and `title` to its name less
        the extension. With `wait`, returns the job's final `ingest_status`;
        without, the `ingest` answer. `ingest_args` are `ingest`'s keywords."""
        file = Path(path)
        # Read off the event loop: a large file must not stall the agent's other calls.
        data = await asyncio.to_thread(file.read_bytes)
        source_uri = source_uri or f"file://{await asyncio.to_thread(file.resolve)}"
        ingest_args.setdefault("title", file.stem)
        up = await self.upload(data, file.name)
        job = await self.ingest(up["upload_id"], source_uri=source_uri, **ingest_args)
        if not wait:
            return job
        return await self.wait_for_ingest(job["ingestion_id"], timeout_s=timeout_s)

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
