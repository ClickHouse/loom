"""A connected publication rechecks authority after embedding and holds it through commit."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Any

import httpx
import pytest

from server.core.errors import ValidationError
from server.core.vectors import codes_state
from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import writes
from server.knowledge.domain.access_list import AccessList, from_json
from server.knowledge.domain.connected_accounts import stopped
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, home, mirror_uri
from server.knowledge.store import blobs, chunks, documents
from server.memory.domain.provenance import pipeline_actor
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_authority import _World as _AuthorityWorld

_ORIGINAL = b"the original connected document"
_CONTENT = "# Connected document\n\nThe owner publishes this revision."
_MIRROR = "acl-" + "a" * 24
_PERMISSIONS = AccessList(owner="person", emails=("reader@example.com",))


class _World(_AuthorityWorld):
    """External embedding/storage writes and the lease store's scheduling boundary."""

    model_name = "publication-test-embedder"
    dim = 4

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient) -> None:
        self.events: list[str] = []
        self.held: dict[asyncio.Task[Any], list[str]] = {}
        super().__init__(monkeypatch, http)
        assert self.source is not None
        self.source = replace(self.source, mirror=_MIRROR)
        self.selected_source = self.source
        self.key = self.source.org, self.source.owner_subject, self.source.kind
        namespace, tag = home(self.source)
        self.request = KnowledgeUpsertInput(
            org=self.source.org,
            namespace=namespace,
            source_uri=mirror_uri("https://docs.example/file", self.source.source_id),
            title="Connected document",
            content=_CONTENT,
            description="An imported source document.",
            tags=[tag],
            metadata={"source_kind": "gdrive", "source_name": "documents"},
            original_bytes_b64=base64.b64encode(_ORIGINAL).decode(),
            original_filename="document.txt",
            blob_content_type="text/plain",
            agent="source-sync-agent",
            session_id="source-sync-pass",
            source_class="imported",
        )
        self.expect_guard = True
        self.pause_embedding = False
        self.pause_document = False
        self.embedding_entered = asyncio.Event()
        self.embedding_release = asyncio.Event()
        self.document_entered = asyncio.Event()
        self.document_release = asyncio.Event()
        self.blob_writes: list[dict[str, Any]] = []
        self.chunk_writes: list[dict[str, Any]] = []
        self.document_writes: list[dict[str, Any]] = []
        self.chunk_retirements: list[list[tuple[str, int]]] = []
        self.embedded: list[list[str]] = []
        monkeypatch.setattr(accounts.leases, "holding", self.holding)
        monkeypatch.setattr(writes, "get_embedder", lambda: self)
        monkeypatch.setattr(
            codes_state,
            "_state",
            codes_state._State(codec_available=False),
        )
        monkeypatch.setattr(chunks, "find_doc_by_content_sha256", self.no_duplicate)
        monkeypatch.setattr(chunks, "count_active_for_doc", self.no_active_chunks)
        monkeypatch.setattr(documents, "get_one", self.no_prior_document)
        monkeypatch.setattr(blobs, "put", self.put_blob)
        monkeypatch.setattr(chunks, "insert_chunks", self.insert_chunks)
        monkeypatch.setattr(documents, "insert_one", self.insert_document)
        monkeypatch.setattr(chunks, "count_matching_chunk_hashes", self.no_matching_chunks)
        monkeypatch.setattr(chunks, "retire_versions", self.retire_versions)

    @asynccontextmanager
    async def lease(self, key: str, **_kw: Any) -> AsyncIterator[None]:
        self.events.append("wait:" + key)
        async with self.locks.setdefault(key, asyncio.Lock()):
            task = asyncio.current_task()
            assert task is not None
            self.holders[task] = self.holders.get(task, 0) + 1
            self.held.setdefault(task, []).append(key)
            self.events.append("enter:" + key)
            try:
                yield
            finally:
                assert self.held[task].pop() == key
                self.holders[task] -= 1
                self.events.append("exit:" + key)

    @asynccontextmanager
    async def holding(self, key: str, **kw: Any) -> AsyncIterator[bool]:
        async with self.lease(key, **kw):
            yield True

    def assert_storage_leases(self) -> None:
        held = self.held.get(asyncio.current_task(), [])
        if not self.expect_guard:
            assert held == []
            return
        assert len(held) == 2
        assert held[0].startswith("connected-account:")
        assert held[1] == "knowledge-source:team:shared-space"

    async def no_duplicate(self, org: str, namespace: str, _sha: str, **_kw: Any):
        assert (org, namespace) == ("team", _MIRROR)
        return None

    async def no_active_chunks(self, org: str, namespace: str, uri: str, **_kw: Any) -> int:
        assert (org, namespace, uri) == ("team", _MIRROR, self.request.source_uri)
        return 0

    async def no_prior_document(self, org: str, namespace: str, uri: str, **kw: Any):
        assert (org, namespace, uri) == ("team", _MIRROR, self.request.source_uri)
        assert kw == {"include_superseded": True}
        return None

    async def put_blob(self, org: str, namespace: str, raw: bytes, **kw: Any) -> str:
        self.assert_storage_leases()
        assert (org, namespace, raw) == ("team", _MIRROR, _ORIGINAL)
        assert kw == {"uploader": "-"}
        self.events.append("blob")
        self.blob_writes.append({"org": org, "namespace": namespace, "raw": raw, **kw})
        return hashlib.sha256(raw).hexdigest()

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        assert self.held.get(asyncio.current_task(), []) == []
        self.events.append("embed-start")
        self.embedded.append(texts)
        self.embedding_entered.set()
        if self.pause_embedding:
            await self.embedding_release.wait()
        self.events.append("embed-end")
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    async def insert_chunks(self, rows: list[dict[str, Any]], **_kw: Any) -> None:
        self.assert_storage_leases()
        self.events.append("chunks")
        self.chunk_writes.extend(dict(row) for row in rows)

    async def insert_document(self, row: dict[str, Any], **kw: Any) -> None:
        self.assert_storage_leases()
        assert kw == {"over": 0, "retire_older": True, "built_from": "", "chunked": True}
        self.events.append("head-start")
        self.document_entered.set()
        if self.pause_document:
            await self.document_release.wait()
        self.assert_storage_leases()
        self.document_writes.append(dict(row))
        self.events.append("head-end")

    async def no_matching_chunks(self, *_args: Any, **_kw: Any) -> int:
        return 0

    async def retire_versions(
        self, org: str, namespace: str, own: list[tuple[str, int]], **_kw: Any
    ) -> None:
        assert (org, namespace) == ("team", _MIRROR)
        self.chunk_retirements.append(own)

    async def await_stage(self, event: asyncio.Event, work: asyncio.Task[Any]) -> None:
        entered = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait({entered, work}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
                pytest.fail("the publication completed before its blocked stage")
        finally:
            if not entered.done():
                entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    async def no_provider(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("publication does not contact a provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_provider)) as http:
        yield _World(monkeypatch, http)


async def _write(state: _World, *, guarded: bool = True):
    with acting_as(None), pipeline_actor(SYNC_WORKER_ACTOR):
        return await writes.handle_knowledge_upsert(
            state.request,
            permissions=_PERMISSIONS,
            commit_guard=partial(accounts.publication_guard, state.selected_source)
            if guarded
            else None,
        )


async def test_a_valid_publication_keeps_real_rows_permissions_and_worker_identity(world) -> None:
    result = await _write(world)
    assert result.chunks_created == len(world.chunk_writes) == 1
    assert len(world.document_writes) == len(world.blob_writes) == 1
    row = world.document_writes[0]
    chunk = world.chunk_writes[0]
    assert row["doc_id"] == result.doc_id
    assert row["content"] == _CONTENT
    assert chunk["content"] == "The owner publishes this revision."
    for stored in (row, chunk):
        assert (stored["org"], stored["namespace"], stored["source_uri"]) == (
            "team",
            _MIRROR,
            world.request.source_uri,
        )
        assert (
            stored["agent"],
            stored["user_id"],
            stored["session_id"],
            stored["source_class"],
        ) == (
            "source-sync-agent",
            "-",
            "source-sync-pass",
            "imported",
        )
        assert stored["tags"] == ["source-id:source-id"]
    assert row["actor"] == SYNC_WORKER_ACTOR
    assert from_json(row["access_json"]) == _PERMISSIONS
    assert json.loads(row["metadata"]) == world.request.metadata
    assert json.loads(chunk["doc_metadata"]) == world.request.metadata
    assert row["version"] == chunk["doc_version"] and row["revision"] == 1
    assert row["blob_path"] == hashlib.sha256(_ORIGINAL).hexdigest()
    assert (row["blob_size"], row["blob_content_type"], row["original_filename"]) == (
        len(_ORIGINAL),
        "text/plain",
        "document.txt",
    )
    assert chunk["embedding"] == [1.0, 0.0, 0.0, 0.0]
    assert chunk["embedding_model"] == result.embedding_model == world.model_name
    assert chunk["embedding_dim"] == result.embedding_dim == 4
    assert json.loads(chunk["chunk_metadata"])["section_path"] == ["Connected document"]
    assert world.embedded == [[chunk["context"] + "\n\n" + chunk["content"]]]
    assert world.events.index("blob") < world.events.index("embed-start")
    assert world.events.index("embed-end") < world.events.index("chunks")
    assert world.events.index("chunks") < world.events.index("head-end")
    assert world.chunk_retirements == []


@pytest.mark.parametrize("change", ("disconnect", "fence-only"))
async def test_standing_lost_during_embedding_prevents_all_final_chunk_and_head_writes(
    world, change: str
) -> None:
    world.pause_embedding = True
    work = asyncio.create_task(_write(world))
    try:
        await world.await_stage(world.embedding_entered, work)
        assert len(world.blob_writes) == 1
        async with accounts.account_lease(*world.key):
            current = world.rows[world.key]
            generation = accounts._next_connection(current.connection_id, inactive=True)
            await accounts._move_fence(current.connection_id, generation)
            if change == "disconnect":
                await world.put(stopped(current, connection_id=generation, at=datetime.now(UTC)))
                assert world.source is not None
                world.source = replace(world.source, active=False)
        world.embedding_release.set()
        with pytest.raises(ValidationError, match="^the connected account is unavailable$"):
            await work
        assert world.chunk_writes == world.document_writes == []
        assert len(world.chunk_retirements) == 1
        assert world.chunk_retirements[0][0][0] == world.request.source_uri
    finally:
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)


async def test_source_replacement_before_the_first_guard_prevents_even_blob_storage(world) -> None:
    assert world.source is not None
    world.source = replace(world.source, source_id="replacement-source")
    with pytest.raises(ValidationError, match="^the connected account is unavailable$"):
        await _write(world)
    assert world.blob_writes == world.chunk_writes == world.document_writes == []
    assert world.embedded == world.chunk_retirements == []


async def test_local_stop_waits_until_the_guarded_head_commit_has_finished(world) -> None:
    world.pause_document = True
    write = asyncio.create_task(_write(world))
    stop: asyncio.Task[Any] | None = None
    attempted = asyncio.Event()

    async def local_stop() -> None:
        attempted.set()
        async with accounts.account_lease(*world.key):
            world.events.append("stop-acquired")
            # The registry deletion is external; this stop has no source left to retire.
            world.source = None
            await accounts._stop_locked(world.rows[world.key])
            world.events.append("stop-finished")

    try:
        await world.await_stage(world.document_entered, write)
        assert len(world.chunk_writes) == 1 and world.document_writes == []
        stop = asyncio.create_task(local_stop())
        await attempted.wait()
        assert not stop.done() and "stop-acquired" not in world.events
        world.document_release.set()
        await write
        await stop
        assert len(world.document_writes) == 1
        assert world.events.index("head-end") < world.events.index("stop-acquired")
        assert not world.rows[world.key].active and not world.rows[world.key].credential_enc
    finally:
        for task in (write, stop):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (write, stop) if t is not None), return_exceptions=True)


async def test_ordinary_upsert_with_no_guard_keeps_its_existing_storage_behavior(world) -> None:
    world.expect_guard = False
    world.source = None
    world.fences.clear()
    result = await _write(world, guarded=False)
    assert result.chunks_created == len(world.chunk_writes) == 1
    assert len(world.blob_writes) == len(world.document_writes) == 1
    assert not any(event.startswith("enter:") for event in world.events)
    assert world.document_writes[0]["content"] == _CONTENT
    assert world.document_writes[0]["actor"] == SYNC_WORKER_ACTOR
