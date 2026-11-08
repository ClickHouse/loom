"""Server-side ingestion: knowledge.ingest / ingest_status / ingest_cancel, the
job runner's stages and outcomes, and the worker pass, over in-memory stores.

The fakes keep every version row an op or the runner writes, so a test reads
the job back as FINAL would (the latest version) and checks what was published
and in what order.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from server.core.errors import (
    BackendTimeoutError,
    BackendUnavailableError,
    NotFoundError,
    OverloadedError,
    PreconditionFailedError,
)
from server.knowledge.api.models_knowledge import (
    KnowledgeIngestCancelInput,
    KnowledgeIngestInput,
    KnowledgeIngestOptions,
    KnowledgeIngestStatusInput,
)
from server.knowledge.app import ingest, ingest_run
from server.knowledge.store import ingestions
from server.knowledge.store.documents import DocumentRow

SQL = b"-- raw events\nCREATE TABLE events (id UInt64) ENGINE = MergeTree ORDER BY id;\nSELECT 1;\n"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class World:
    """What the stores hold: version rows of jobs, uploads, originals, chunk and
    document inserts, supersessions, and the order they were written in."""

    jobs: list[dict[str, Any]] = field(default_factory=list)
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    originals: dict[str, bytes] = field(default_factory=dict)
    documents: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[dict[str, Any]] = field(default_factory=list)
    superseded: list[tuple[str, int]] = field(default_factory=list)
    retired: list[int] = field(default_factory=list)  # one chunk version each
    dead: set[int] = field(default_factory=set)  # every chunk version retired, however
    cancels: list[str] = field(default_factory=list)  # ingestion ids with a cancel
    fail_document: Exception | None = None
    late_documents: list[dict[str, Any]] = field(default_factory=list)  # timed out, land later
    claim_busy: bool = False  # another write holds the source's claim
    on_chunks: Any = None  # replaces the chunk insert, to act between it and the document row
    claim_cuts_insert: bool = False  # the claim's deadline cuts the document insert off
    cancel_insert: bool = False  # the run is cancelled with the document insert in flight
    left_to_expire: int = 0  # job leases a run left to expire rather than release
    lease_held: bool = True  # the job lease as `held` reads it
    lose_lease_in_claim: bool = False  # the job lease is lost once the claim is taken
    fail_retire_older: Exception | None = None  # raised once, by the next older-chunks retire
    in_claim: Any = None  # runs as the source's claim is taken
    fail_dataset_rows: int = 0  # dataset row inserts that land before one fails
    log: list[str] = field(default_factory=list)
    stored_vectors: dict[str, list[float]] = field(default_factory=dict)
    embedded: list[str] = field(default_factory=list)
    fail_embed: Exception | None = None
    catalog: list[dict[str, Any]] = field(default_factory=list)  # loom.datasets version rows
    dataset_rows: list[dict[str, Any]] = field(default_factory=list)
    forgotten: list[tuple[str, int, int]] = field(default_factory=list)  # (uri, before, only)

    def served(self, uri: str, doc_version: int) -> list[dict[str, Any]]:
        """The catalog as FINAL reads it at `doc_version`."""
        latest: dict[str, dict[str, Any]] = {}
        for r in self.catalog:
            if r["source_uri"] == uri and r["doc_version"] == doc_version:
                if (
                    r["dataset_id"] not in latest
                    or r["version"] > latest[r["dataset_id"]]["version"]
                ):
                    latest[r["dataset_id"]] = r
        return list(latest.values())

    def retire_source(self, uri: str, before_version: int | None = None) -> None:
        """Every chunk version of the source (older than `before_version`) is
        retired, as a publish, a text write or a delete retires them."""
        self.dead |= {
            c["doc_version"]
            for c in self.chunks
            if c["source_uri"] == uri
            and (before_version is None or c["doc_version"] < before_version)
        }

    def job(self, ingestion_id: str) -> dict[str, Any] | None:
        rows = [r for r in self.jobs if r["ingestion_id"] == ingestion_id]
        return max(rows, key=lambda r: r["version"]) if rows else None


class _Embedder:
    model_name = "fake-ngram-8"
    dim = 8

    def __init__(self, world: World) -> None:
        self.world = world

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if self.world.fail_embed is not None:
            raise self.world.fail_embed
        self.world.embedded.extend(texts)
        return [[float(len(t))] * 8 for t in texts]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    from server.core import leases
    from server.core.vectors import codes_state
    from server.knowledge.store import blobs
    from server.knowledge.store import chunks as chunks_repo
    from server.knowledge.store import datasets as datasets_repo
    from server.knowledge.store import documents as documents_repo

    async def insert_ingestion(row: dict[str, Any], **_: Any) -> None:
        w.jobs.append(dict(row))
        w.log.append(f"job:{row['state']}:{row['stage']}")

    async def get_ingestion(
        org: str, ns: str, ingestion_id: str, **_: Any
    ) -> dict[str, Any] | None:
        row = w.job(ingestion_id)
        return dict(row) if row and (row["org"], row["namespace"]) == (org, ns) else None

    async def get_upload(org: str, ns: str, upload_id: str, **_: Any) -> dict[str, Any] | None:
        row = w.uploads.get(upload_id)
        return dict(row) if row and (row["org"], row["namespace"]) == (org, ns) else None

    async def read(org: str, ns: str, sha: str, **_: Any) -> bytes:
        return w.originals.get(sha, b"")

    async def put(org: str, ns: str, data: bytes, **_: Any) -> str:
        sha = hashlib.sha256(data).hexdigest()
        w.originals[sha] = data
        w.log.append("artifact")
        return sha

    async def locate(org: str, ns: str, sha: str, **_: Any) -> Any:
        return blobs.Stored(len(w.originals[sha]), "") if sha in w.originals else None

    async def vectors_by_input(
        org: str, ns: str, model: str, dim: int, shas: list[str], **_: Any
    ) -> dict[str, list[float]]:
        return {s: w.stored_vectors[s] for s in shas if s in w.stored_vectors}

    async def insert_chunks(rows: list[dict[str, Any]], **_: Any) -> int:
        (w.on_chunks or w.chunks.extend)(rows)
        w.log.append("chunks")
        return len(rows)

    async def insert_one(row: dict[str, Any], **_: Any) -> None:
        if w.fail_document is not None:
            w.late_documents.append(dict(row))
            raise w.fail_document
        if w.claim_cuts_insert:
            w.late_documents.append(dict(row))
            raise TimeoutError  # asyncio.timeout around the claimed block
        if w.cancel_insert:
            w.late_documents.append(dict(row))
            raise asyncio.CancelledError
        w.documents.append(dict(row))
        w.log.append("document")

    async def get_one(org: str, ns: str, uri: str, **_: Any) -> DocumentRow | None:
        rows = [d for d in w.documents if d["source_uri"] == uri]
        if not rows:
            return None
        d = max(rows, key=lambda r: r["version"])
        return DocumentRow(**{k: d[k] for k in DocumentRow.model_fields if k in d})

    async def mark_doc_superseded(
        org: str,
        ns: str,
        uri: str,
        *,
        before_version: int | None = None,
        only_version: int | None = None,
        **_: Any,
    ) -> None:
        if only_version is not None:
            w.retired.append(only_version)
            w.dead.add(only_version)
            w.log.append("retire")
            return
        if w.fail_retire_older is not None:
            e, w.fail_retire_older = w.fail_retire_older, None
            raise e
        w.superseded.append((uri, before_version or 0))
        w.retire_source(uri, before_version)
        w.log.append("supersede")

    async def refuse_width(*a: Any, **k: Any) -> None:
        return None

    class _Lease:
        @property
        def held(self) -> bool:
            return w.lease_held

        def let_expire(self) -> None:
            w.left_to_expire += 1

        async def release(self) -> None:
            return None

        @asynccontextmanager
        async def renewed(self) -> Any:
            yield self

    async def take(*a: Any, **k: Any) -> Any:
        return _Lease()

    async def release_shielded(lease: Any) -> None:
        return None

    @asynccontextmanager
    async def holding(name: str, **k: Any) -> Any:
        if w.in_claim is not None:
            w.in_claim()
        if w.lose_lease_in_claim:
            w.lease_held = False
        try:
            yield not w.claim_busy
        except TimeoutError as e:  # as leases.holding turns its deadline
            raise leases.ClaimExpired(f"{name} cut off", operation=name) from e

    async def insert_cancel(org: str, ns: str, ingestion_id: str, author: Any, **_: Any) -> None:
        w.cancels.append(ingestion_id)

    async def cancel_requested(org: str, ns: str, ingestion_id: str, **_: Any) -> bool:
        return ingestion_id in w.cancels

    monkeypatch.setattr(ingestions, "insert_cancel", insert_cancel)
    monkeypatch.setattr(ingestions, "cancel_requested", cancel_requested)
    monkeypatch.setattr(ingestions, "insert_ingestion", insert_ingestion)
    monkeypatch.setattr(ingestions, "get_ingestion", get_ingestion)
    monkeypatch.setattr(ingestions, "get_upload", get_upload)
    monkeypatch.setattr(blobs, "read", read)
    monkeypatch.setattr(blobs, "put", put)
    monkeypatch.setattr(blobs, "locate", locate)
    monkeypatch.setattr(chunks_repo, "vectors_by_input", vectors_by_input)
    monkeypatch.setattr(chunks_repo, "insert_chunks", insert_chunks)

    async def count_active_for_doc(org: str, ns: str, uri: str, **_: Any) -> int:
        return sum(1 for c in w.chunks if c["source_uri"] == uri and c["doc_version"] not in w.dead)

    monkeypatch.setattr(chunks_repo, "count_active_for_doc", count_active_for_doc)

    async def count_version(org: str, ns: str, uri: str, version: int, **_: Any) -> int:
        if version in w.dead:
            return 0
        return sum(1 for c in w.chunks if c["source_uri"] == uri and c["doc_version"] == version)

    monkeypatch.setattr(chunks_repo, "count_version", count_version)
    monkeypatch.setattr(chunks_repo, "mark_doc_superseded", mark_doc_superseded)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    monkeypatch.setattr(documents_repo, "get_one", get_one)

    async def at_version(
        org: str, ns: str, uri: str, doc_version: int, *, dataset_id: str = "", **_: Any
    ) -> list[dict[str, Any]]:
        rows = w.served(uri, doc_version)
        return [dict(r) for r in rows if not dataset_id or r["dataset_id"] == dataset_id]

    async def insert_catalog(rows: list[dict[str, Any]], **_: Any) -> None:
        w.catalog.extend(dict(r) for r in rows)
        w.log.append("datasets")

    async def insert_rows(rows: list[dict[str, Any]], **_: Any) -> None:
        if w.fail_dataset_rows:
            w.fail_dataset_rows -= 1
            if not w.fail_dataset_rows:
                raise BackendUnavailableError("insert refused", operation="insert")
        w.dataset_rows.extend(dict(r) for r in rows)

    async def forget(
        org: str, ns: str, uris: Any, *, before: int = 0, only: int = 0, **_: Any
    ) -> None:
        # As the store does: the rows of the datasets the catalog names.
        ids = {r["dataset_id"] for r in w.catalog if r["source_uri"] in uris}

        def gone(r: dict[str, Any]) -> bool:
            if r["dataset_id"] not in ids or r["source_uri"] not in uris:
                return False
            v = r["doc_version"]
            return v < before if before else v == only if only else True

        w.forgotten.extend((u, before, only) for u in uris)
        w.catalog = [r for r in w.catalog if not gone(r)]
        w.dataset_rows = [r for r in w.dataset_rows if not gone(r)]

    async def forget_published(org: str, ns: str, through: dict[str, int], **_: Any) -> None:
        ids = {r["dataset_id"] for r in w.catalog if r["source_uri"] in through}

        def gone(r: dict[str, Any]) -> bool:
            uri = r["source_uri"]
            return r["dataset_id"] in ids and uri in through and r["doc_version"] <= through[uri]

        # As the store does: a snapshot a catalog row past the bound serves keeps its rows.
        kept = {r["snapshot_id"] for r in w.catalog if r["source_uri"] in through and not gone(r)}
        w.forgotten.extend((u, 0, v) for u, v in through.items())
        w.catalog = [r for r in w.catalog if not gone(r)]
        w.dataset_rows = [r for r in w.dataset_rows if not gone(r) or r["snapshot_id"] in kept]

    monkeypatch.setattr(datasets_repo, "forget_published", forget_published)
    monkeypatch.setattr(datasets_repo, "at_version", at_version)
    monkeypatch.setattr(datasets_repo, "insert_catalog", insert_catalog)
    monkeypatch.setattr(datasets_repo, "insert_rows", insert_rows)
    monkeypatch.setattr(datasets_repo, "forget", forget)

    async def revision_by_ingestion(org: str, ns: str, uri: str, job_id: str, **_: Any) -> Any:
        rows = [
            d for d in w.documents if d["source_uri"] == uri and d.get("ingestion_id") == job_id
        ]
        return max(rows, key=lambda r: r["version"])["revision"] if rows else None

    monkeypatch.setattr(documents_repo, "revision_by_ingestion", revision_by_ingestion)
    monkeypatch.setattr(codes_state, "refuse_width", refuse_width)
    monkeypatch.setattr(leases, "take", take)
    monkeypatch.setattr(leases, "release_shielded", release_shielded)
    monkeypatch.setattr(leases, "holding", holding)
    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Embedder(w))
    # The reference extractor, served in process: the job sends it the bytes.
    import httpx

    from loom_extractor import service
    from server.knowledge.app import extractors
    from server.settings import settings

    token = "t" * 24
    app = service.create_app(service.Config(token=token))
    monkeypatch.setattr(extractors, "transport", httpx.ASGITransport(app=app))
    monkeypatch.setattr(settings, "document_extractor_url", "http://extractor:8790")
    monkeypatch.setattr(settings, "document_extractor_token", token)
    extractors._declared.clear()
    return w


def _upload(w: World, data: bytes = SQL, filename: str = "retention.sql", **over: Any) -> str:
    sha = hashlib.sha256(data).hexdigest()
    w.originals[sha] = data
    upload_id = f"11111111-1111-1111-1111-{len(w.uploads):012d}"
    w.uploads[upload_id] = {
        "org": "o",
        "namespace": "ns",
        "upload_id": upload_id,
        "sha256": sha,
        "size": len(data),
        "filename": filename,
        "content_type": "application/sql",
        "format": "sql",
        "agent": "-",
        "user_id": "-",
        "actor": "",
        "created_at": _now(),
        "expires_at": _now() + timedelta(hours=1),
        **over,
    }
    return upload_id


def _ingest(w: World, upload_id: str, **over: Any) -> str:
    args = {
        "org": "o",
        "namespace": "ns",
        "upload_id": upload_id,
        "source_uri": "repo://retention.sql",
    }
    out = asyncio.run(ingest.handle_knowledge_ingest(KnowledgeIngestInput(**{**args, **over})))
    assert out.state == "pending"
    return out.ingestion_id


def _run(w: World, ingestion_id: str) -> str | None:
    """A worker pass running the job once it is due: a retry delay is taken as passed."""
    job = w.job(ingestion_id)
    if job is not None and job["state"] == "pending" and job["next_attempt_at"] > _now():
        job["next_attempt_at"] = _now() - timedelta(seconds=1)
    return asyncio.run(ingest_run.take_and_run(w.job(ingestion_id)))  # type: ignore[arg-type]


# ---- knowledge.ingest ----------------------------------------------------------


def test_an_ingest_names_its_upload_and_carries_its_options_and_authorship(world: World) -> None:
    job_id = _ingest(
        world,
        _upload(world),
        title="Retention",
        tags=["ops"],
        metadata={"team": "data"},
        expected_revision=0,
        options=KnowledgeIngestOptions(sql_dialect="clickhouse"),
        session_id="s1",
    )
    job = world.job(job_id)
    assert job is not None
    assert (job["state"], job["source_uri"], job["filename"], job["title"]) == (
        "pending",
        "repo://retention.sql",
        "retention.sql",
        "Retention",
    )
    assert json.loads(job["options"])["sql_dialect"] == "clickhouse"
    assert job["expected_revision"] == 0 and job["session_id"] == "s1" and job["tags"] == ["ops"]
    assert job["sha256"] == world.uploads[job["upload_id"]]["sha256"]


def test_an_ingest_refuses_a_missing_foreign_or_expired_upload(world: World) -> None:
    with pytest.raises(NotFoundError):
        _ingest(world, "not-a-uuid")
    foreign = _upload(world, actor="user:someone-else")
    with pytest.raises(NotFoundError):
        _ingest(world, foreign)
    expired = _upload(world, expires_at=_now() - timedelta(seconds=1))
    with pytest.raises(PreconditionFailedError):
        _ingest(world, expired)
    assert world.jobs == []


def test_a_title_defaults_to_the_file_name(world: World) -> None:
    job = world.job(_ingest(world, _upload(world)))
    assert job is not None and job["title"] == "retention.sql"


# ---- the runner ------------------------------------------------------------------


def test_a_job_publishes_chunks_then_the_document_then_retires_older_versions(world: World) -> None:
    job_id = _ingest(
        world, _upload(world), options=KnowledgeIngestOptions(sql_dialect="clickhouse")
    )
    assert _run(world, job_id) == "ready"
    order = [e for e in world.log if not e.startswith("job:")]
    assert order == ["artifact", "chunks", "document", "supersede"]
    (doc,) = world.documents
    assert doc["revision"] == 1 and doc["format"] == "sql" and doc["ingestion_id"] == job_id
    assert doc["blob_path"] == world.uploads[world.job(job_id)["upload_id"]]["sha256"]  # type: ignore[index]
    assert doc["artifact_sha256"] in world.originals and doc["parser"].startswith("loom-sql/")
    assert json.loads(doc["coverage"])["complete"] is True
    assert {c["doc_version"] for c in world.chunks} == {doc["version"]}
    assert world.superseded == [("repo://retention.sql", doc["version"])]
    create = next(c for c in world.chunks if "CREATE TABLE" in c["content"])
    assert "events" in create["keys"]
    meta = json.loads(create["chunk_metadata"])
    assert meta["locator"]["line_start"] == 1 and meta["block_type"] == "sql_statement"
    job = world.job(job_id)
    assert job is not None and (job["state"], job["revision"], job["chunks"], job["attempts"]) == (
        "ready",
        1,
        len(world.chunks),
        1,
    )


def test_the_artifact_is_the_extraction_json_and_the_text_is_rendered_from_it(world: World) -> None:
    from server.knowledge.domain.extract import parse_artifact

    _run(world, _ingest(world, _upload(world, b"# Runbook\n\nStep one.\n", "runbook.md")))
    (doc,) = world.documents
    stored = world.originals[doc["artifact_sha256"]]
    artifact = parse_artifact(stored, fmt="markdown", max_bytes=len(stored))
    assert artifact.format == "markdown" and doc["content"] == "# Runbook\n\nStep one."


def test_a_second_ingest_is_the_next_revision_and_reuses_identical_vectors(world: World) -> None:
    _run(world, _ingest(world, _upload(world)))
    world.stored_vectors = {c["embed_input_sha256"]: c["embedding"] for c in world.chunks}
    world.embedded.clear()
    _run(world, _ingest(world, _upload(world)))
    assert [d["revision"] for d in world.documents] == [1, 2]
    assert world.embedded == [], "the same inputs under the same model reuse their vectors"


def test_an_extractor_that_cannot_be_reached_leaves_the_job_to_retry(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage, not the source: the job goes back with a delay, nothing is
    published, and the earlier revision keeps serving."""
    import httpx

    from server.knowledge.app import extractors

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setattr(extractors, "transport", httpx.MockTransport(unreachable))
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "pending"
    job = world.job(job_id)
    assert job is not None and job["attempts"] == 1 and job["error_code"] == "retrying"
    assert world.documents == [] and world.chunks == []


@pytest.mark.parametrize(
    ("data", "filename", "over", "code"),
    [
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1old", "old.bin", {}, "unsupported_format"),
        (b"PAR1 not read PAR1", "a.parquet", {}, "corrupt"),
        (b"%PDF-1.7 ...", "a.pdf", {}, "corrupt"),
        ("café".encode("latin-1"), "a.md", {}, "encoding"),
        (
            b"SELECT 1;\nNOT SQL AT ALL;\n",
            "q.sql",
            {"options": KnowledgeIngestOptions(sql_dialect="clickhouse")},
            "incomplete_extraction",
        ),
        (b"\n\n  \n", "blank.md", {}, "no_text"),
    ],
)
def test_a_file_that_cannot_be_published_fails_with_its_code(
    world: World, data: bytes, filename: str, over: dict[str, Any], code: str
) -> None:
    job_id = _ingest(world, _upload(world, data, filename), **over)
    assert _run(world, job_id) == "failed"
    job = world.job(job_id)
    assert job is not None and job["error_code"] == code
    assert world.documents == [] and world.chunks == []


def test_an_incomplete_extraction_publishes_as_partial_when_allowed(world: World) -> None:
    job_id = _ingest(
        world,
        _upload(world, b"SELECT 1;\nNOT SQL AT ALL;\n", "q.sql"),
        allow_partial=True,
        options=KnowledgeIngestOptions(sql_dialect="clickhouse"),
    )
    assert _run(world, job_id) == "partial"
    assert json.loads(world.documents[0]["coverage"])["complete"] is False


def test_a_source_past_the_chunk_limit_fails_too_large(
    world: World, settings_override: Any
) -> None:
    settings_override(knowledge_ingest_max_chunks=1)
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "failed" and world.job(job_id)["error_code"] == "too_large"  # type: ignore[index]


def test_an_expected_revision_that_moved_fails_and_leaves_the_live_revision(world: World) -> None:
    _run(world, _ingest(world, _upload(world)))
    job_id = _ingest(world, _upload(world), expected_revision=0)
    assert _run(world, job_id) == "failed"
    job = world.job(job_id)
    assert job is not None and job["error_code"] == "revision_conflict"
    assert [d["revision"] for d in world.documents] == [1]
    assert _run(world, _ingest(world, _upload(world), expected_revision=1)) == "ready"


def test_an_expected_revision_of_0_refuses_a_live_document_numbered_0(world: World) -> None:
    """A document written before revisions were numbered, or by an older
    build, is live at revision 0: 0 still means "no live document"."""
    _run(world, _ingest(world, _upload(world)))
    world.documents[-1].update(revision=0, ingestion_id="")
    job_id = _ingest(world, _upload(world), expected_revision=0)
    assert _run(world, job_id) == "failed"
    job = world.job(job_id)
    assert job is not None and job["error_code"] == "revision_conflict"
    assert "revision 0" in job["error_message"] and len(world.documents) == 1


def test_a_missing_original_fails_original_gone(world: World) -> None:
    upload_id = _upload(world)
    world.originals.clear()
    job_id = _ingest(world, upload_id)
    assert _run(world, job_id) == "failed" and world.job(job_id)["error_code"] == "original_gone"  # type: ignore[index]


def test_a_transient_failure_retries_with_a_growing_delay_then_fails(
    world: World, settings_override: Any
) -> None:
    settings_override(knowledge_ingest_max_attempts=2)
    world.fail_embed = BackendUnavailableError("provider down")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "pending"
    job = world.job(job_id)
    assert job is not None and job["error_code"] == "retrying" and job["next_attempt_at"] > _now()
    assert _run(world, job_id) == "failed"
    assert world.job(job_id)["error_code"] == "retries_exhausted"  # type: ignore[index]
    assert world.documents == []


def test_a_defect_fails_the_job_instead_of_stalling_the_queue(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise KeyError("boom")

    monkeypatch.setattr(ingest_run, "chunk_artifact", broken)
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "failed" and world.job(job_id)["error_code"] == "internal"  # type: ignore[index]


def test_a_job_no_longer_open_or_held_elsewhere_is_not_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import leases

    job_id = _ingest(world, _upload(world))
    _run(world, job_id)
    stale = dict(world.jobs[0])
    assert asyncio.run(ingest_run.take_and_run(stale)) is None, "it is ready now"

    async def busy(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(leases, "take", busy)
    other = _ingest(world, _upload(world))
    assert _run(world, other) is None and world.job(other)["state"] == "pending"  # type: ignore[index]


# ---- cancel and status ---------------------------------------------------------


def test_a_pending_job_cancels_at_once_and_a_running_one_before_it_publishes(world: World) -> None:
    pending = _ingest(world, _upload(world))
    out = asyncio.run(
        ingest.handle_knowledge_ingest_cancel(
            KnowledgeIngestCancelInput(org="o", namespace="ns", ingestion_id=pending)
        )
    )
    assert (out.state, out.cancelled) == ("cancelled", True)
    assert _run(world, pending) == "cancelled" and world.documents == []

    running = _ingest(world, _upload(world))
    real_vectors = ingest_run._vectors

    async def cancel_while_embedding(job: dict[str, Any], inputs: list[str]) -> Any:
        await ingest.handle_knowledge_ingest_cancel(
            KnowledgeIngestCancelInput(org="o", namespace="ns", ingestion_id=running)
        )
        return await real_vectors(job, inputs)

    ingest_run._vectors = cancel_while_embedding  # type: ignore[assignment]
    try:
        assert _run(world, running) == "cancelled"
    finally:
        ingest_run._vectors = real_vectors  # type: ignore[assignment]
    assert world.documents == [] and world.chunks == []


def test_a_finished_job_is_not_cancelled(world: World) -> None:
    job_id = _ingest(world, _upload(world))
    _run(world, job_id)
    out = asyncio.run(
        ingest.handle_knowledge_ingest_cancel(
            KnowledgeIngestCancelInput(org="o", namespace="ns", ingestion_id=job_id)
        )
    )
    assert (out.found, out.state, out.cancelled) == (True, "ready", False)


def test_status_reports_the_job_and_hides_another_namespaces(world: World) -> None:
    job_id = _ingest(world, _upload(world))
    before = asyncio.run(
        ingest.handle_knowledge_ingest_status(
            KnowledgeIngestStatusInput(org="o", namespace="ns", ingestion_id=job_id)
        )
    )
    assert (before.state, before.retrieval, before.revision) == ("pending", "pending", 0)
    _run(world, job_id)
    after = asyncio.run(
        ingest.handle_knowledge_ingest_status(
            KnowledgeIngestStatusInput(org="o", namespace="ns", ingestion_id=job_id)
        )
    )
    assert (after.state, after.retrieval, after.revision, after.format) == (
        "ready",
        "ready",
        1,
        "sql",
    )
    assert after.coverage and after.coverage["unit"] == "statements" and after.chunks > 0
    other = asyncio.run(
        ingest.handle_knowledge_ingest_status(
            KnowledgeIngestStatusInput(org="o", namespace="elsewhere", ingestion_id=job_id)
        )
    )
    assert other.found is False


# ---- the worker pass -------------------------------------------------------------


def test_the_ingest_worker_runs_on_its_settings(settings_override: Any) -> None:
    """knowledge_ingest_enabled switches the worker; its cadence, idle cadence
    and pass deadline are knowledge_ingest_interval_seconds,
    knowledge_ingest_idle_interval_seconds and knowledge_ingest_max_pass_seconds."""
    from server.workers import registry

    settings_override(
        knowledge_ingest_enabled=True,
        knowledge_ingest_interval_seconds=3,
        knowledge_ingest_idle_interval_seconds=30,
        knowledge_ingest_max_pass_seconds=1200,
    )
    spec = next(w for w in registry.enabled_workers() if w.name == "knowledge_ingest")
    assert (spec.interval_seconds, spec.idle_interval_seconds) == (3, 30)
    assert registry.pass_deadline_seconds("knowledge_ingest") == 1200
    settings_override(knowledge_ingest_enabled=False)
    assert "knowledge_ingest" not in {w.name for w in registry.enabled_workers()}


def test_the_job_is_read_again_under_its_lease_with_sequential_consistency(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker takes the next job at once, so the run that ended a job may have
    written on another replica a moment ago: the re-read under the lease must see it,
    or a job already ended reads as pending and runs again."""
    from server.core.ch import sequential_reads_active

    read = ingestions.get_ingestion
    consistent: list[bool] = []

    async def recording(*a: Any, **k: Any) -> Any:
        consistent.append(sequential_reads_active())
        return await read(*a, **k)

    job_id = _ingest(world, _upload(world))
    monkeypatch.setattr(ingestions, "get_ingestion", recording)
    assert _run(world, job_id) == "ready"
    assert consistent and consistent[0] is True  # the re-read under the lease


def test_a_job_put_back_to_wait_is_not_run_from_a_due_row_read_before_it(
    world: World, settings_override: Any
) -> None:
    """A due row a lagging replica served names the job as it was; under its lease
    the job reads as waiting out a retry delay, so it is left for its time."""
    settings_override(knowledge_ingest_max_attempts=3)
    world.fail_embed = BackendUnavailableError("provider down")
    job_id = _ingest(world, _upload(world))
    stale = dict(world.job(job_id))  # type: ignore[arg-type]  # as due() read it, due now
    assert _run(world, job_id) == "pending"
    waiting = world.job(job_id)
    assert waiting is not None and waiting["next_attempt_at"] > _now()
    world.fail_embed = None
    assert asyncio.run(ingest_run.take_and_run(stale)) is None
    assert world.job(job_id) == waiting  # untouched: no attempt spent, no run


def test_a_fresh_job_written_by_a_clock_ahead_of_the_workers_still_runs(world: World) -> None:
    """Its due time is the writer's clock and has not moved since the due row was
    read: the worker runs it, whatever its own clock says."""
    job_id = _ingest(world, _upload(world))
    job = world.job(job_id)
    assert job is not None
    job["next_attempt_at"] = _now() + timedelta(seconds=3)  # the writer's clock runs ahead
    assert asyncio.run(ingest_run.take_and_run(dict(job))) == "ready"


def test_by_default_the_worker_takes_the_next_job_at_once_and_waits_only_when_idle() -> None:
    """A pass that ran a job is followed at once, so a queue drains at the speed of
    its jobs; one that found none waits the idle interval."""
    from server.workers import pass_context, registry
    from server.workers.supervisor import next_interval_seconds

    spec = next(w for w in registry.enabled_workers() if w.name == "knowledge_ingest")
    busy = pass_context.PassContext("knowledge_ingest")
    idle = pass_context.PassContext("knowledge_ingest", idle=True)
    assert next_interval_seconds(spec, busy) == 0
    assert next_interval_seconds(spec, idle) == 15


def test_a_pass_runs_one_job_passing_over_one_another_process_holds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One job per pass, so a long job has the whole pass deadline and the jobs
    after it are not cut off; a job another process holds is passed over."""
    from server.core import leases
    from server.knowledge.workers import ingest as worker

    held = _ingest(world, _upload(world))
    ok = _ingest(world, _upload(world))
    later = _ingest(world, _upload(world))
    real_take = leases.take

    async def take(name: str, **k: Any) -> Any:
        return None if name.endswith(held) else await real_take(name, **k)

    async def due(limit: int, **_: Any) -> list[dict[str, Any]]:
        return [dict(world.job(i)) for i in (held, ok, later)][:limit]  # type: ignore[arg-type]

    monkeypatch.setattr(leases, "take", take)
    monkeypatch.setattr(ingestions, "due", due)
    result = asyncio.run(worker.run_pass())
    assert result.state == "ready"
    assert [world.job(i)["state"] for i in (held, ok, later)] == [  # type: ignore[index]
        "pending",
        "ready",
        "pending",
    ]


def test_a_job_whose_runs_never_finished_fails_once_its_attempts_are_spent(
    world: World, settings_override: Any
) -> None:
    """A pass cut off at its deadline (or a process that died) writes no
    failure; the attempt it started still counts, and the job fails before
    another extraction and embedding once they are spent."""
    settings_override(knowledge_ingest_max_attempts=2)
    job_id = _ingest(world, _upload(world))
    job = world.job(job_id)
    assert job is not None
    world.jobs.append(
        {
            **job,
            "state": "processing",
            "stage": "embed",
            "attempts": 2,
            "version": job["version"] + 1,
        }
    )
    assert _run(world, job_id) == "failed"
    assert world.job(job_id)["error_code"] == "retries_exhausted"  # type: ignore[index]
    assert world.embedded == [] and world.chunks == []


def test_a_job_row_keeps_every_column_across_versions() -> None:
    """loom.knowledge_ingestions is a ReplacingMergeTree read with FINAL: every
    version row the builder makes carries every column forward."""
    first = ingestions.ingestion_row(
        org="o",
        namespace="ns",
        ingestion_id="22222222-2222-2222-2222-222222222222",
        source_uri="u",
        upload_id="33333333-3333-3333-3333-333333333333",
        sha256="a" * 64,
        state="pending",
        title="T",
        tags=["x"],
    )
    later = ingestions.ingestion_row(first, state="processing", stage="parse")
    assert later["title"] == "T" and later["tags"] == ["x"] and later["version"] > first["version"]
    assert set(later) == set(ingestions.INGESTION_COLUMNS)
    with pytest.raises(ValueError, match="not a column"):
        ingestions.ingestion_row(first, bogus=1)


# ---- POST /v1/knowledge.upload ---------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, world: World) -> Any:
    from fastapi.testclient import TestClient

    from server.main import app
    from server.settings import settings

    async def insert_upload(row: dict[str, Any], **_: Any) -> None:
        world.uploads[row["upload_id"]] = dict(row)

    monkeypatch.setattr(ingestions, "insert_upload", insert_upload)
    monkeypatch.setattr(settings, "auth_required", False)
    return TestClient(app, base_url="http://localhost")  # auth off answers only a local Host


def _post(client: Any, data: bytes, **params: str) -> Any:
    query = {"org": "o", "namespace": "ns", "filename": "retention.sql", **params}
    return client.post("/v1/knowledge.upload", params=query, content=data)


def test_an_upload_stores_the_bytes_and_answers_a_scoped_id(client: Any, world: World) -> None:
    r = _post(client, SQL, sha256=hashlib.sha256(SQL).hexdigest(), content_type="application/sql")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sha256"] == hashlib.sha256(SQL).hexdigest() and body["size"] == len(SQL)
    assert body["format"] == "sql" and body["filename"] == "retention.sql"
    row = world.uploads[body["upload_id"]]
    assert (row["org"], row["namespace"], row["sha256"], row["content_type"]) == (
        "o",
        "ns",
        body["sha256"],
        "application/sql",
    )
    assert world.originals[body["sha256"]] == SQL
    assert row["expires_at"] > row["created_at"]


def test_an_upload_registers_its_namespace_before_it_writes(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.app.namespace_autoregister import should_autoregister
    from server.knowledge.api.ops_knowledge import knowledge_ingest
    from server.knowledge.app import upload_routes
    from server.knowledge.store import blobs

    assert should_autoregister(knowledge_ingest, "o", "ns")

    order: list[str] = []

    async def autoregister(op: Any, *, org: str, namespace: str, auth: Any) -> bool:
        order.append(f"register {op.name} {org}/{namespace}")
        return False

    async def write(*_: Any, **__: Any) -> None:
        order.append("write")

    monkeypatch.setattr(upload_routes, "maybe_autoregister", autoregister)
    monkeypatch.setattr(blobs, "put", write)
    monkeypatch.setattr(ingestions, "insert_upload", write)
    assert _post(client, SQL).status_code == 201
    assert order == ["register knowledge.ingest o/ns", "write", "write"]


def test_an_upload_expires_after_knowledge_upload_ttl_seconds(
    client: Any, world: World, settings_override: Any
) -> None:
    settings_override(knowledge_upload_ttl_seconds=600)
    row = world.uploads[_post(client, SQL).json()["upload_id"]]
    assert row["expires_at"] - row["created_at"] == timedelta(seconds=600)


def test_an_upload_names_its_format_without_reading_the_bytes(client: Any, world: World) -> None:
    """The server parses no original: plain text named .pdf is a pdf upload, and
    the extractor refuses it at ingest."""
    r = _post(client, b"plain text", filename="report.pdf")
    assert r.status_code == 201, r.text
    assert r.json()["format"] == "pdf"


def test_an_upload_keeps_only_the_file_name_of_a_path(client: Any, world: World) -> None:
    r = _post(client, SQL, filename="../../etc/retention.sql")
    assert r.status_code == 201 and r.json()["filename"] == "retention.sql"


@pytest.mark.parametrize(
    ("data", "params", "status", "code"),
    [
        (b"", {}, 400, "VALIDATION_FAILED"),
        (SQL, {"sha256": "0" * 64}, 422, "CHECKSUM_MISMATCH"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1old", {"filename": "old.doc"}, 415, "UNSUPPORTED_FILE"),
        (SQL, {"filename": "  "}, 400, "VALIDATION_FAILED"),
    ],
)
def test_an_upload_refuses_what_it_cannot_take(
    client: Any, world: World, data: bytes, params: dict[str, str], status: int, code: str
) -> None:
    r = _post(client, data, **params)
    assert r.status_code == status and r.json()["detail"]["code"] == code
    assert world.uploads == {}


def test_an_upload_past_the_size_limit_is_refused_before_it_is_stored(
    client: Any, world: World, settings_override: Any
) -> None:
    settings_override(knowledge_upload_max_bytes=10)
    r = _post(client, SQL)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "PAYLOAD_TOO_LARGE"
    assert world.uploads == {} and hashlib.sha256(SQL).hexdigest() not in world.originals


def test_an_upload_over_the_request_cap_is_stored_whole_under_its_own_limit(
    client: Any, world: World
) -> None:
    """The request-body cap passes the upload route through: a chunked body
    larger than LOOM_MAX_REQUEST_BYTES (and under the upload limit) is stored
    whole, not cut off at the cap."""
    from server.settings import settings

    size = settings.max_request_bytes + 1024
    assert size <= settings.knowledge_upload_max_bytes

    def chunks() -> Any:
        sent = 0
        while sent < size:
            part = b"x" * min(1 << 20, size - sent)
            sent += len(part)
            yield part

    query = {"org": "o", "namespace": "ns", "filename": "big.txt"}
    r = client.post("/v1/knowledge.upload", params=query, content=chunks())
    assert r.status_code == 201, r.text
    assert r.json()["size"] == size and len(world.originals[r.json()["sha256"]]) == size


def test_an_upload_is_admitted_as_an_op_is(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    """An org at its in-flight cap, or a caller over its rate limit, is
    refused 429 before the body is read."""
    from server.core import ratelimit, tenant_slots

    settings_override(tenant_max_concurrent_requests=1, replica_count=1, web_workers=1)
    monkeypatch.setitem(tenant_slots._IN_FLIGHT, "o", 1)
    r = _post(client, SQL)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "RATE_LIMITED"
    assert r.headers["Retry-After"] == "1"
    monkeypatch.delitem(tenant_slots._IN_FLIGHT, "o")

    class _Empty:
        def check(self, namespace: str, op_name: str, **_: Any) -> Any:
            assert (namespace, op_name) == ("ns", "knowledge.add")
            return ratelimit.CheckResult(
                allowed=False, retry_after_s=2.5, limit=1, remaining=0, reset_s=3
            )

    monkeypatch.setattr(ratelimit, "get_limiter", lambda: _Empty())
    r = _post(client, SQL)
    assert r.status_code == 429 and r.headers["Retry-After"] == "3"
    assert world.uploads == {} and hashlib.sha256(SQL).hexdigest() not in world.originals


def test_an_upload_is_admitted_when_the_limiter_fails_and_the_failure_is_counted(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import fail_open, ratelimit

    class _Broken:
        def check(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("bucket table unreadable")

    monkeypatch.setattr(ratelimit, "get_limiter", lambda: _Broken())
    before = fail_open.counts().get("rate_limit", 0)
    assert _post(client, SQL).status_code == 201
    assert fail_open.counts()["rate_limit"] == before + 1


def test_a_job_erased_while_it_runs_publishes_nothing_and_writes_no_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An erasure deletes the job's row mid-run: the run stops, publishes no
    chunk or document, stores no artifact and does not write the row back."""
    job_id = _ingest(world, _upload(world))
    real = ingestions.get_ingestion
    reads = 0

    async def erased_after_start(org: str, ns: str, ingestion_id: str, **k: Any) -> Any:
        nonlocal reads
        reads += 1
        return await real(org, ns, ingestion_id, **k) if reads <= 2 else None

    monkeypatch.setattr(ingestions, "get_ingestion", erased_after_start)
    rows_before = len(world.jobs)
    assert _run(world, job_id) is None
    assert world.documents == [] and world.chunks == [] and "artifact" not in world.log
    assert len(world.jobs) == rows_before + 1  # only the `processing` row before the erasure


def test_a_run_that_lost_its_lease_publishes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import leases

    class _Lost:
        held = False

        async def release(self) -> None:
            return None

        @asynccontextmanager
        async def renewed(self) -> Any:
            yield self

    async def take(*a: Any, **k: Any) -> Any:
        return _Lost()

    monkeypatch.setattr(leases, "take", take)
    assert _run(world, _ingest(world, _upload(world))) is None
    assert world.documents == [] and world.chunks == []


def test_a_job_that_published_before_its_last_save_is_not_published_again(world: World) -> None:
    """A crash between the publish and the final save: the next run finds its
    own revision live and finishes without a second revision."""
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    chunks = len(world.chunks)
    job = world.job(job_id)
    assert job is not None
    world.jobs.append(
        {**job, "state": "processing", "stage": "publish", "version": job["version"] + 1}
    )
    assert _run(world, job_id) == "ready"
    assert len(world.chunks) == chunks and len(world.documents) == 1
    latest = world.job(job_id)
    assert latest is not None and latest["revision"] == 1


def test_a_publish_that_stops_after_its_chunks_retires_them(
    world: World, settings_override: Any
) -> None:
    """The chunks are live once inserted: a publish that fails after them and
    before its document insert (the source's claim busy) retires them when the
    job fails, and a retry that publishes retires the earlier attempt's before
    it inserts its own."""
    settings_override(knowledge_ingest_max_attempts=1)
    world.claim_busy = True
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "failed"
    job = world.job(job_id)
    assert job is not None and job["error_code"] == "retries_exhausted"
    assert world.retired == [world.chunks[0]["doc_version"]] == [job["doc_version"]]
    assert world.documents == []

    settings_override(knowledge_ingest_max_attempts=2)
    world.retired = []
    again = _ingest(world, _upload(world))
    assert _run(world, again) == "pending"
    first = world.chunks[-1]["doc_version"]
    assert world.retired == [first]
    world.claim_busy = False
    assert _run(world, again) == "ready"
    published = world.job(again)["doc_version"]  # type: ignore[index]
    # Retired again before the new insert: a no-op, unless an insert landed late.
    assert set(world.retired) == {first} and published != first
    assert world.documents[-1]["version"] == published


def test_a_pending_job_cancelled_after_a_publish_that_stopped_serves_none_of_its_chunks(
    world: World,
) -> None:
    """The retry that puts the job back to pending retires the chunks its
    publish inserted, so a cancel of the pending job, which ends it without
    another run, leaves none live."""
    world.claim_busy = True
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "pending"
    asyncio.run(
        ingest.handle_knowledge_ingest_cancel(
            KnowledgeIngestCancelInput(org="o", namespace="ns", ingestion_id=job_id)
        )
    )
    status = asyncio.run(
        ingest.handle_knowledge_ingest_status(
            KnowledgeIngestStatusInput(org="o", namespace="ns", ingestion_id=job_id)
        )
    )
    assert (status.state, status.retrieval, status.cancel_requested) == (
        "cancelled",
        "cancelled",
        True,
    )
    assert world.retired == [world.chunks[0]["doc_version"]] and world.documents == []
    assert _run(world, job_id) == "cancelled" and world.job(job_id)["state"] == "cancelled"  # type: ignore[index]


@pytest.mark.parametrize(
    "error", [BackendUnavailableError("store down"), RuntimeError("a defect")], ids=str
)
@pytest.mark.parametrize("attempts", [1, 5])
def test_a_failure_after_the_document_row_ends_the_job_published(
    world: World, settings_override: Any, attempts: int, error: Exception
) -> None:
    """The document row landed and the retire of the earlier revision's chunks
    failed: on any attempt, last or not, the job ends published (the retire run
    again), not failed or back to pending."""
    settings_override(knowledge_ingest_max_attempts=attempts)
    _run(world, _ingest(world, _upload(world)))
    world.fail_retire_older = error
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None and (job["revision"], job["error_code"]) == (2, "")
    assert job["doc_version"] not in world.dead
    assert [d["revision"] for d in world.documents] == [1, 2]
    assert world.superseded[-1] == ("repo://retention.sql", world.documents[-1]["version"])


def test_an_ingest_a_later_text_write_overtook_fails_and_retires_its_chunks(world: World) -> None:
    """A text write takes no claim: one that took its version after this
    publish's would outrank its row, so the ingest fails `revision_conflict`
    rather than report a revision no reader sees."""
    _run(world, _ingest(world, _upload(world)))
    world.documents[-1]["version"] = 2**62  # a write stamped later than any job's version
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "failed"
    job = world.job(job_id)
    assert job is not None and job["error_code"] == "revision_conflict"
    assert world.retired == [job["doc_version"]] and len(world.documents) == 1


def test_a_reindex_of_an_ingested_document_is_refused_and_writes_nothing(world: World) -> None:
    """Its chunks come from the artifact; re-cutting the rendered text would
    drop their keys and lines and the document's format, parser and coverage."""
    from server.core.errors import ValidationError
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput
    from server.knowledge.app.writes import handle_knowledge_update_metadata

    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    documents, chunks = len(world.documents), len(world.chunks)
    edit = KnowledgeUpdateMetadataInput(
        org="o", namespace="ns", source_uri="repo://retention.sql", title="New", reindex=True
    )
    with pytest.raises(ValidationError, match="knowledge.ingest"):
        asyncio.run(handle_knowledge_update_metadata(edit))
    assert (len(world.documents), len(world.chunks)) == (documents, chunks)


def test_a_cancel_writes_no_version_of_the_job(world: World) -> None:
    """A cancel is a row of its own: a job version written from the caller's
    read would replace what the worker saved after it (the chunk version it
    recorded, or the state it ended in)."""
    job_id = _ingest(world, _upload(world))
    job = world.job(job_id)
    assert job is not None
    world.jobs.append(
        {**job, "state": "processing", "doc_version": 7, "version": job["version"] + 1}
    )
    rows = len(world.jobs)
    out = asyncio.run(
        ingest.handle_knowledge_ingest_cancel(
            KnowledgeIngestCancelInput(org="o", namespace="ns", ingestion_id=job_id)
        )
    )
    assert (out.state, out.cancelled) == ("processing", True)
    assert len(world.jobs) == rows and world.cancels == [job_id]
    assert world.job(job_id)["doc_version"] == 7  # type: ignore[index]


def test_a_job_that_published_ends_ready_after_a_later_cancel(world: World) -> None:
    """Its own publish is checked before a cancel: a revision it published
    stays published, and the job says so."""
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None
    world.jobs.append({**job, "state": "processing", "version": job["version"] + 1})
    world.cancels.append(job_id)
    assert _run(world, job_id) == "ready" and world.job(job_id)["revision"] == 1  # type: ignore[index]
    assert len(world.documents) == 1 and world.retired == []


@pytest.mark.parametrize("after", ["a metadata edit", "a delete"])
def test_a_rerun_of_a_published_job_keeps_its_chunks_and_publishes_nothing_more(
    world: World, after: str
) -> None:
    """An edit writes a new document version and a delete a tombstone (and
    retires the chunks), both naming the job that published: the rerun knows
    its own publish, retires none of its chunks and brings nothing back."""
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None
    doc = world.documents[-1]
    edit = {"version": doc["version"] + 1}
    if after == "a delete":
        edit["superseded_at"] = _now()
        world.retire_source("repo://retention.sql")
    world.documents.append({**doc, **edit})
    world.jobs.append({**job, "state": "processing", "version": job["version"] + 1})
    world.fail_embed = BackendUnavailableError("provider down")  # a rerun that works fails
    assert _run(world, job_id) == "ready"
    assert world.retired == [] and len(world.documents) == 2
    assert world.superseded[-1] == ("repo://retention.sql", job["doc_version"])


@pytest.mark.parametrize("merged", [False, True])
def test_a_rerun_of_a_published_job_after_a_text_write_publishes_nothing_more(
    world: World, merged: bool
) -> None:
    """A text write names no job, and retires the job's chunks. While the job's
    own version row is still there the rerun finds it and ends published (with
    no chunk of its own live: the text write replaced them); once
    a merge has folded it into the text write's, the rerun cannot tell, and a
    later write landed after its chunks, so it fails `revision_conflict`. In
    neither case does it publish the older file again."""
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None
    doc = world.documents[-1]
    world.documents.append(
        {**doc, "version": doc["version"] + 1, "revision": 2, "ingestion_id": ""}
    )
    world.retire_source("repo://retention.sql")  # the text write retires the earlier chunks
    if merged:
        world.documents.remove(doc)
    world.jobs.append(
        {
            **job,
            "state": "processing",
            "stage": "publish",
            "chunks": 0,
            "version": job["version"] + 1,
        }
    )
    chunks, world.embedded = len(world.chunks), []
    if merged:
        assert _run(world, job_id) == "failed"
        assert world.job(job_id)["error_code"] == "revision_conflict"  # type: ignore[index]
    else:
        assert _run(world, job_id) == "ready"
        assert (world.job(job_id)["revision"], world.job(job_id)["chunks"]) == (1, 0)  # type: ignore[index]
    assert len(world.chunks) == chunks and world.embedded == []
    assert [d["revision"] for d in world.documents][-1] == 2


def test_the_retry_delay_stops_doubling_at_an_hour(world: World, settings_override: Any) -> None:
    """Many attempts never push a retry toward the job row's 30-day TTL."""
    settings_override(knowledge_ingest_max_attempts=30)
    world.fail_embed = BackendUnavailableError("provider down")
    job_id = _ingest(world, _upload(world))
    job = world.job(job_id)
    assert job is not None
    world.jobs.append({**job, "attempts": 20, "version": job["version"] + 1})
    assert _run(world, job_id) == "pending"
    wait = world.job(job_id)["next_attempt_at"] - _now()  # type: ignore[index]
    assert timedelta(minutes=59) < wait <= timedelta(hours=1)


def test_a_retry_publishes_over_a_write_that_landed_while_it_waited(world: World) -> None:
    """A job a retry put back to pending is not judged as a run that stopped
    mid-publish, so a metadata edit while it waits does not fail it."""
    _run(world, _ingest(world, _upload(world)))
    world.claim_busy = True
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "pending"
    doc = world.documents[-1]
    world.documents.append({**doc, "title": "Edited", "version": time.time_ns()})
    world.claim_busy = False
    assert _run(world, job_id) == "ready"
    assert world.job(job_id)["revision"] == 2  # type: ignore[index]


def test_a_document_insert_that_timed_out_and_landed_later_ends_the_job_published(
    world: World, settings_override: Any
) -> None:
    """A client-side timeout does not stop the insert: the run stops as a crash
    would and leaves the job processing at its publish stage, chunks live. When
    the row has landed the next run finishes from the job's own chunk version:
    the earlier revision's chunks retired, its own live and counted. When it
    has not, the next run retires them and the job fails."""
    settings_override(knowledge_ingest_max_attempts=1)
    _run(world, _ingest(world, _upload(world)))
    world.fail_document = BackendTimeoutError("timed out")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "processing" and world.retired == []
    job = world.job(job_id)
    assert job is not None and (job["state"], job["stage"]) == ("processing", "publish")
    version = job["doc_version"]
    world.fail_document = None
    world.documents.extend(world.late_documents)  # the insert lands
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None and (job["revision"], job["doc_version"]) == (2, version)
    own = sum(1 for c in world.chunks if c["doc_version"] == version)
    assert job["chunks"] == own > 0 and version not in world.retired
    assert world.superseded[-1] == ("repo://retention.sql", version)

    world.fail_document, world.late_documents = BackendTimeoutError("timed out"), []
    lost = _ingest(world, _upload(world))
    assert _run(world, lost) == "processing"
    world.fail_document = None  # the insert never landed
    assert _run(world, lost) == "failed"
    assert world.job(lost)["error_code"] == "retries_exhausted"  # type: ignore[index]
    assert world.retired == [world.job(lost)["doc_version"]]  # type: ignore[index]


@pytest.mark.parametrize("merged", [False, True])
def test_a_document_deleted_after_a_late_insert_stays_deleted(world: World, merged: bool) -> None:
    """The insert timed out and landed, the writer deleted the document, then
    the job runs again. The delete's tombstone names the job (a delete carries
    `ingestion_id` forward), so even once a merge has folded the job's own row
    into it the job ends published, and the document stays deleted."""
    _run(world, _ingest(world, _upload(world)))
    world.fail_document = BackendTimeoutError("timed out")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "processing"
    world.fail_document = None
    world.documents.extend(world.late_documents)  # the insert lands
    late = world.documents[-1]
    world.documents.append({**late, "version": time.time_ns(), "superseded_at": _now()})
    world.retire_source("repo://retention.sql")  # the delete retires every chunk
    if merged:
        world.documents.remove(late)
    documents, chunks = len(world.documents), len(world.chunks)
    assert _run(world, job_id) == "ready" and world.job(job_id)["revision"] == 2  # type: ignore[index]
    assert (len(world.documents), len(world.chunks)) == (documents, chunks)
    assert world.documents[-1]["superseded_at"] is not None


def test_a_cancel_after_an_uncertain_insert_waits_for_the_job_to_settle(world: World) -> None:
    """The job stopped mid-publish, so a cancel answers `processing` (the
    worker stops it before it publishes, if it can): its row may already have
    landed, and then the job ends published."""
    _run(world, _ingest(world, _upload(world)))
    world.fail_document = BackendTimeoutError("timed out")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "processing"
    out = asyncio.run(
        ingest.handle_knowledge_ingest_cancel(
            KnowledgeIngestCancelInput(org="o", namespace="ns", ingestion_id=job_id)
        )
    )
    assert (out.state, out.cancelled) == ("processing", True)
    world.fail_document = None
    world.documents.extend(world.late_documents)
    assert _run(world, job_id) == "ready" and world.job(job_id)["revision"] == 2  # type: ignore[index]


def test_a_job_erased_after_its_chunks_writes_no_document_row(world: World) -> None:
    """An erasure that deletes the job's row between its chunk insert and its
    document row: no document row is written for the erased scope."""
    job_id = _ingest(world, _upload(world))
    real_insert = world.chunks.extend

    def erase_job(rows: Any) -> None:
        real_insert(rows)
        world.jobs[:] = [j for j in world.jobs if j["ingestion_id"] != job_id]

    world.on_chunks = erase_job
    assert _run(world, job_id) is None
    assert world.documents == [] and world.job(job_id) is None


def test_a_document_insert_the_claims_deadline_cut_off_stops_the_run(
    world: World, settings_override: Any
) -> None:
    """The source claim's deadline can cut the insert off after it was sent:
    the row may still land, so the run stops as for a timeout, even on the
    last attempt, rather than retire the chunks and fail the job."""
    settings_override(knowledge_ingest_max_attempts=1)
    _run(world, _ingest(world, _upload(world)))
    world.claim_cuts_insert = True
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "processing" and world.retired == []
    world.claim_cuts_insert = False
    world.documents.extend(world.late_documents)  # the insert lands
    assert _run(world, job_id) == "ready" and world.job(job_id)["revision"] == 2  # type: ignore[index]


def test_a_store_out_of_slots_puts_the_job_back(world: World) -> None:
    """A pool or bucket with no slot free now is a transient failure: the job
    retries, it does not fail as a defect."""
    world.fail_embed = OverloadedError("no slot")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "pending"
    assert world.job(job_id)["error_code"] == "retrying"  # type: ignore[index]


def test_a_rerun_comes_only_after_an_uncertain_insert_has_settled(world: World) -> None:
    """A run that gave up on an uncertain document insert leaves the job's lease
    to expire. Its last `held` check is inside the claimed block, the lease
    renewed within half its TTL before it, and the send comes within the
    block's bound of that check, so no other worker takes the job until the
    insert has landed or stopped: a carried send starts within `_TRANSIT_S`,
    runs within a claimed statement's limit, and commits within
    `_RELEASE_MARGIN_S` more."""
    from server.core import leases, rmt_carry

    earliest_takeover = ingest_run.JOB_LEASE_S / 2 - ingest_run._SOURCE_CLAIM_S * leases._WORK_SHARE
    insert_bound = (
        rmt_carry._TRANSIT_S
        + ingest_run._SOURCE_CLAIM_S * leases._STATEMENT_SHARE
        + rmt_carry._RELEASE_MARGIN_S
    )
    assert earliest_takeover > insert_bound

    world.fail_document = BackendTimeoutError("timed out")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "processing" and world.left_to_expire == 1


def test_a_run_cancelled_with_its_insert_in_flight_leaves_the_lease_to_expire(
    world: World,
) -> None:
    """A pass deadline or a shutdown that cancels the run mid-insert: the
    cancel goes on, the job stays processing, and its lease is not released."""
    world.cancel_insert = True
    job_id = _ingest(world, _upload(world))
    with pytest.raises(asyncio.CancelledError):
        _run(world, job_id)
    job = world.job(job_id)
    assert job is not None and (job["state"], job["stage"]) == ("processing", "publish")
    assert world.left_to_expire == 1


def test_the_last_lease_check_is_inside_the_claimed_block(world: World) -> None:
    """The timing bound counts from a `held` check inside the claimed block: a
    lease lost once the claim is taken stops the publish before its document
    row."""
    world.lose_lease_in_claim = True
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) is None and world.documents == []


def test_each_published_chunk_records_its_revision_and_format(world: World) -> None:
    """Search and citations read a chunk's revision off the chunk: it is the revision
    its document row takes, not whichever is live when it is read."""
    _run(world, _ingest(world, _upload(world)))
    _run(world, _ingest(world, _upload(world)))
    by_version: dict[int, set[tuple[int, str]]] = {}
    for c in world.chunks:
        meta = json.loads(c["chunk_metadata"])
        by_version.setdefault(c["doc_version"], set()).add((meta["revision"], meta["format"]))
    assert sorted(by_version.values(), key=str) == [{(1, "sql")}, {(2, "sql")}]
    assert [d["revision"] for d in world.documents] == [1, 2]


def test_a_write_between_the_revision_and_the_claim_fails_the_publish(world: World) -> None:
    """The chunks recorded a revision decided before they were written; a text write
    of the source landing before the claim makes it stale, so the publish fails and
    its chunks are retired."""
    _run(world, _ingest(world, _upload(world)))

    def text_write_lands(rows: Any) -> None:
        world.chunks.extend(rows)
        doc = world.documents[-1]
        world.documents.append(
            {**doc, "version": doc["version"] + 1, "revision": 2, "ingestion_id": ""}
        )

    world.on_chunks = text_write_lands
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "failed"
    job = world.job(job_id)
    assert job is not None and job["error_code"] == "revision_conflict"
    assert world.retired == [job["doc_version"]]


# ---- datasets ------------------------------------------------------------------

CSV = b"region,amount,day\nEU,10.5,2026-01-02\nUS,7,2026-01-03\n"


def _csv_job(w: World, data: bytes = CSV) -> str:
    upload = _upload(w, data, "sales.csv", content_type="text/csv", format="csv")
    return _ingest(w, upload, source_uri="repo://sales.csv")


def test_a_csv_ingest_publishes_its_table_as_a_typed_dataset_before_the_document(
    world: World,
) -> None:
    assert _run(world, _csv_job(world)) == "ready"
    assert world.log.index("datasets") < world.log.index("document")
    doc = world.documents[-1]
    (catalog,) = world.served("repo://sales.csv", doc["version"])
    assert (catalog["source_uri"], catalog["doc_version"]) == ("repo://sales.csv", doc["version"])
    assert catalog["revision"] == doc["revision"] == 1
    assert catalog["artifact_sha256"] == doc["artifact_sha256"]
    assert catalog["schema_state"] == "inferred" and catalog["row_count"] == 2
    schema = {c["name"]: (c["type"], c["slot"]) for c in json.loads(catalog["schema"])}
    assert schema == {"region": ("string", "s1"), "amount": ("number", "n1"), "day": ("date", "t1")}
    rows = sorted(world.dataset_rows, key=lambda r: r["row_id"])
    assert [(r["s1"], r["n1"], r["t1"]) for r in rows] == [
        ("EU", Decimal("10.500000000"), datetime(2026, 1, 2)),
        ("US", Decimal("7.000000000"), datetime(2026, 1, 3)),
    ]
    assert all(r["snapshot_id"] == catalog["snapshot_id"] for r in rows)
    job = world.job(world.jobs[-1]["ingestion_id"])
    assert job is not None
    for c in ("agent", "user_id", "session_id", "actor", "source_class"):
        assert catalog[c] == rows[0][c] == job[c]
    # Once the row lands, the source's older snapshots go.
    assert world.forgotten == [("repo://sales.csv", doc["version"], 0)]


def test_a_new_revision_keeps_an_approval_of_the_same_columns_and_drops_the_old_snapshot(
    world: World,
) -> None:
    assert _run(world, _csv_job(world)) == "ready"
    (first,) = world.served("repo://sales.csv", world.documents[-1]["version"])
    schema = json.loads(first["schema"])
    schema[1]["type"] = "string"  # approved: amount as text
    world.catalog.append(
        {**first, "schema": json.dumps(schema), "schema_state": "approved",
         "approved_by": "user:a", "version": first["version"] + 1}
    )  # fmt: skip
    assert _run(world, _csv_job(world, CSV + b"FR,3,2026-01-04\n")) == "ready"
    doc = world.documents[-1]
    (second,) = world.served("repo://sales.csv", doc["version"])
    assert second["schema_state"] == "approved" and second["approved_by"] == "user:a"
    assert [c["type"] for c in json.loads(second["schema"])] == ["string", "string", "date"]
    assert {r["doc_version"] for r in world.dataset_rows} == {doc["version"]}
    assert sorted(r["s2"] for r in world.dataset_rows) == ["10.5", "3", "7"]
    assert world.served("repo://sales.csv", first["doc_version"]) == []


def test_a_new_revision_with_other_columns_is_inferred_again(world: World) -> None:
    assert _run(world, _csv_job(world)) == "ready"
    (first,) = world.served("repo://sales.csv", world.documents[-1]["version"])
    world.catalog.append({**first, "schema_state": "approved", "version": first["version"] + 1})
    assert _run(world, _csv_job(world, b"region,total\nEU,1\n")) == "ready"
    (second,) = world.served("repo://sales.csv", world.documents[-1]["version"])
    assert second["schema_state"] == "inferred" and second["approved_by"] == ""


def test_a_failed_publish_retires_its_own_datasets(world: World) -> None:
    world.fail_document = ValueError("defect")
    assert _run(world, _csv_job(world)) == "failed"
    version = world.late_documents[-1]["version"]
    assert ("repo://sales.csv", 0, version) in world.forgotten
    assert world.catalog == [] and world.dataset_rows == []


def test_a_publish_that_fails_midway_through_a_tables_rows_deletes_them(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.app import datasets

    monkeypatch.setattr(datasets, "_BATCH", 1)
    world.fail_dataset_rows = 2  # the first batch lands, the second fails
    job_id = _csv_job(world)
    assert _run(world, job_id) == "pending"
    assert world.job(job_id)["error_code"] == "retrying"  # type: ignore[index]
    # The empty catalog row written first names the dataset, so its rows are found.
    assert world.dataset_rows == [] and world.catalog == []


def test_an_approval_landing_while_a_revision_publishes_is_carried_by_its_retry(
    world: World,
) -> None:
    assert _run(world, _csv_job(world)) == "ready"
    (first,) = world.served("repo://sales.csv", world.documents[-1]["version"])
    schema = json.loads(first["schema"])
    schema[1]["type"] = "string"

    def approve() -> None:  # the approval takes the claim first
        world.in_claim = None
        world.catalog.append(
            {**first, "schema": json.dumps(schema), "schema_state": "approved",
             "version": first["version"] + 1}
        )  # fmt: skip

    world.in_claim = approve
    job_id = _csv_job(world, CSV + b"FR,3,2026-01-04\n")
    assert _run(world, job_id) == "pending"
    job = world.job(job_id)
    assert job is not None
    # Run again at once, the attempt not counted: another write stopped it, not a failure.
    assert (job["attempts"], job["error_code"]) == (0, "")
    assert job["next_attempt_at"] <= _now()
    assert world.documents[-1]["version"] == first["doc_version"]  # nothing published
    assert _run(world, job_id) == "ready"
    (second,) = world.served("repo://sales.csv", world.documents[-1]["version"])
    assert second["schema_state"] == "approved"
    assert [c["type"] for c in json.loads(second["schema"])] == ["string", "string", "date"]


def test_an_erasure_between_a_tables_batches_stops_its_rows(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.app import datasets

    monkeypatch.setattr(datasets, "_BATCH", 1)
    job_id = _csv_job(world)
    inserted = 0

    async def erased_after_one(rows: list[dict[str, Any]], **_: Any) -> None:
        nonlocal inserted
        inserted += 1
        world.dataset_rows.extend(dict(r) for r in rows)
        # The erasure deletes the job (and the rows so far) after the first batch.
        world.jobs = [j for j in world.jobs if j["ingestion_id"] != job_id]
        world.dataset_rows.clear()

    monkeypatch.setattr(datasets.datasets_repo, "insert_rows", erased_after_one)
    assert _run(world, job_id) is None  # JobGone: the run writes nothing more
    assert inserted == 1 and world.dataset_rows == []


def test_a_delete_during_a_first_ingest_leaves_the_running_jobs_datasets_whole(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete takes no claim, so it may land between a first ingest's batches;
    it retires no document version there, so it deletes none of the job's rows,
    and the dataset the job serves holds every row it counts."""
    from server.knowledge.api.models_knowledge import KnowledgeDeleteInput
    from server.knowledge.app import datasets
    from server.knowledge.app.writes import handle_knowledge_delete
    from server.knowledge.store import datasets as datasets_repo

    monkeypatch.setattr(datasets, "_BATCH", 1)
    retired: list[dict[str, int]] = []

    async def forget_published(org: str, ns: str, through: dict[str, int], **_: Any) -> None:
        retired.append(dict(through))

    monkeypatch.setattr(datasets_repo, "forget_published", forget_published)
    insert = datasets_repo.insert_rows
    deleted = False

    async def delete_after_one(rows: list[dict[str, Any]], **_: Any) -> None:
        nonlocal deleted
        await insert(rows)
        if not deleted:
            deleted = True
            delete = KnowledgeDeleteInput(org="o", namespace="ns", source_uri="repo://sales.csv")
            await handle_knowledge_delete(delete)

    monkeypatch.setattr(datasets_repo, "insert_rows", delete_after_one)
    assert _run(world, _csv_job(world)) == "ready"
    assert deleted and retired == []
    (served,) = world.served("repo://sales.csv", world.documents[-1]["version"])
    assert served["row_count"] == 2 == len(world.dataset_rows)


def test_a_delete_of_a_deleted_source_clears_a_snapshot_left_behind(world: World) -> None:
    """A snapshot an earlier delete could not reach (an approval's, landing as it
    ran) goes with the next delete, though the source is already gone."""
    from server.knowledge.api.models_knowledge import KnowledgeDeleteInput
    from server.knowledge.app.writes import handle_knowledge_delete

    assert _run(world, _csv_job(world)) == "ready"
    version = world.documents[-1]["version"]
    delete = KnowledgeDeleteInput(org="o", namespace="ns", source_uri="repo://sales.csv")
    assert asyncio.run(handle_knowledge_delete(delete)).deleted
    assert world.catalog == [] and world.dataset_rows == []
    world.catalog.append({"source_uri": "repo://sales.csv", "dataset_id": "ds_x",
                          "doc_version": version, "snapshot_id": 7, "version": 1})  # fmt: skip
    world.dataset_rows.append({"source_uri": "repo://sales.csv", "dataset_id": "ds_x",
                               "doc_version": version, "snapshot_id": 7})  # fmt: skip
    again = asyncio.run(handle_knowledge_delete(delete))
    assert not again.deleted
    assert world.catalog == [] and world.dataset_rows == []


def test_forget_takes_one_version_bound() -> None:
    from server.knowledge.store import datasets as datasets_repo

    for bounds in ({}, {"before": 2, "only": 1}):
        with pytest.raises(ValueError, match="before or only"):
            asyncio.run(datasets_repo.forget("o", "ns", ["u"], **bounds))


def test_a_metadata_edit_keeps_serving_the_datasets_and_their_approval(world: World) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput
    from server.knowledge.app.writes import handle_knowledge_update_metadata

    assert _run(world, _csv_job(world)) == "ready"
    before = world.documents[-1]["version"]
    (first,) = world.served("repo://sales.csv", before)
    approved = {**first, "schema_state": "approved", "version": first["version"] + 1}
    world.catalog.append(approved)
    edit = KnowledgeUpdateMetadataInput(
        org="o", namespace="ns", source_uri="repo://sales.csv", title="Sales 2026"
    )
    assert asyncio.run(handle_knowledge_update_metadata(edit)).updated
    after = world.documents[-1]
    assert after["title"] == "Sales 2026" and after["version"] > before
    (carried,) = world.served("repo://sales.csv", after["version"])
    assert (carried["snapshot_id"], carried["schema_state"]) == (first["snapshot_id"], "approved")
    # The next revision reads the approval off the live (edited) version.
    assert _run(world, _csv_job(world, CSV + b"FR,3,2026-01-04\n")) == "ready"
    (second,) = world.served("repo://sales.csv", world.documents[-1]["version"])
    assert second["schema_state"] == "approved"


def test_a_metadata_edit_of_an_ingested_source_waits_for_its_claim(world: World) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput
    from server.knowledge.app.writes import handle_knowledge_update_metadata

    assert _run(world, _csv_job(world)) == "ready"
    rows = len(world.documents)
    world.claim_busy = True
    edit = KnowledgeUpdateMetadataInput(
        org="o", namespace="ns", source_uri="repo://sales.csv", title="Sales 2026"
    )
    with pytest.raises(BackendUnavailableError):
        asyncio.run(handle_knowledge_update_metadata(edit))
    assert len(world.documents) == rows


def test_a_metadata_edit_overtaken_while_it_waited_for_the_claim_is_refused(
    world: World,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeUpdateMetadataInput
    from server.knowledge.app.writes import handle_knowledge_update_metadata

    assert _run(world, _csv_job(world)) == "ready"
    live = world.documents[-1]
    catalog = list(world.catalog)

    def published_meanwhile() -> None:  # another write landed before the claim was taken
        world.in_claim = None
        world.documents.append({**live, "version": live["version"] + 1, "title": "Other"})

    world.in_claim = published_meanwhile
    edit = KnowledgeUpdateMetadataInput(
        org="o", namespace="ns", source_uri="repo://sales.csv", title="Sales 2026"
    )
    with pytest.raises(PreconditionFailedError):
        asyncio.run(handle_knowledge_update_metadata(edit))
    assert world.documents[-1]["title"] == "Other" and world.catalog == catalog


# ---- faults: each one leaves the earlier revision serving ----------------------
#
# Revision 1 is published, then revision 2 meets one fault. The job ends in a
# typed state (with one attempt allowed, a transient fault ends
# `retries_exhausted`; the retry itself is tested above) and revision 1's
# document row and chunks are the ones served. The table in
# docs/architecture/document-extraction.md ("Faults") names each case.


def _answer(status: int, body: bytes, **headers: str) -> Any:
    """An extractor's answer as the network gives one: unread, so the client
    reads it as a stream (httpx reads a response built from `content` at once)."""
    import httpx

    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def _extract_answers(monkeypatch: pytest.MonkeyPatch, answer: Any, *, slow_s: float = 0.0) -> None:
    """The extractor's capabilities as the reference service declares them, and
    its /v1/extract answered by `answer(reference_response) -> httpx.Response`."""
    import httpx

    from server.knowledge.app import extractors

    reference = extractors.transport
    assert reference is not None

    class Answering(httpx.AsyncBaseTransport):
        # Not httpx.MockTransport: that reads each response, and the client
        # reads an extractor's answer as a raw stream.
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await reference.handle_async_request(request)
            if not request.url.path.endswith("/v1/extract"):
                return response
            await response.aread()
            if slow_s:
                await asyncio.sleep(slow_s)
            return answer(response)

    monkeypatch.setattr(extractors, "transport", Answering())


def _refused(code: str) -> Any:
    body = json.dumps({"error": {"code": code, "message": f"refused: {code}"}}).encode()
    return lambda _: _answer(422, body)


def _artifact_edited(edit: Any) -> Any:
    import httpx

    def answer(response: httpx.Response) -> httpx.Response:
        artifact = response.json()
        edit(artifact)
        return _answer(200, json.dumps(artifact).encode())

    return answer


def _second_block_parent(artifact: dict[str, Any]) -> None:
    artifact["blocks"][1]["parent"] = "no-such-block"


def _first_block_past_the_source(artifact: dict[str, Any]) -> None:
    artifact["blocks"][0]["locator"] = {"line_start": 1, "line_end": 10_000}


def _gzipped(response: Any) -> Any:
    import gzip

    return _answer(200, gzip.compress(response.content), **{"Content-Encoding": "gzip"})


def _fault(name: str, w: World, monkeypatch: pytest.MonkeyPatch, settings_override: Any) -> None:
    import httpx

    from server.core.errors import EmbedTimeoutError
    from server.core.object_store import ObjectStoreError
    from server.knowledge.app import extractors
    from server.knowledge.store import blobs

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    async def bucket_fails(*a: Any, **k: Any) -> bytes:
        raise ObjectStoreError("get failed: ServiceUnavailable", operation="read")

    async def bucket_refuses_put(*a: Any, **k: Any) -> str:
        raise ObjectStoreError("put failed: ServiceUnavailable", operation="put")

    if name == "extractor down":
        monkeypatch.setattr(extractors, "transport", httpx.MockTransport(unreachable))
    elif name == "extractor past the timeout":
        settings_override(document_extractor_timeout_s=0.2)
        _extract_answers(monkeypatch, lambda r: _answer(200, r.content), slow_s=1.0)
    elif name == "service timed the source out":
        _extract_answers(monkeypatch, _refused("timeout"))
    elif name == "service has no parser for the format":
        _extract_answers(monkeypatch, _refused("format_unavailable"))
    elif name == "artifact over the bound":
        settings_override(document_extractor_max_artifact_bytes=1024)
        _extract_answers(monkeypatch, lambda _: _answer(200, b"{" + b" " * 4096))
    elif name == "artifact not JSON":
        _extract_answers(monkeypatch, lambda _: _answer(200, b"<html>proxy</html>"))
    elif name == "artifact out of contract":
        _extract_answers(monkeypatch, _artifact_edited(lambda a: a.update(schema_version=99)))
    elif name == "block under a missing parent":
        _extract_answers(monkeypatch, _artifact_edited(_second_block_parent))
    elif name == "locator past the source":
        _extract_answers(monkeypatch, _artifact_edited(_first_block_past_the_source))
    elif name == "compressed answer":
        _extract_answers(monkeypatch, _gzipped)
    elif name == "no extractor configured":
        settings_override(document_extractor_url="")
    elif name == "bucket fails the original's read":
        monkeypatch.setattr(blobs, "read", bucket_fails)
    elif name == "bucket refuses the artifact's write":
        monkeypatch.setattr(blobs, "put", bucket_refuses_put)
    elif name == "document row refused after the chunks":
        w.fail_document = ValueError("the document row is refused")
    elif name == "embedder down":
        w.fail_embed = BackendUnavailableError("provider down", operation="embed")
    elif name == "embedder past its deadline":
        w.fail_embed = EmbedTimeoutError("embedding took past its deadline")
    elif name == "embedder defect":
        w.fail_embed = ValueError("the provider answered 3 vectors for 4 inputs")
    else:
        raise AssertionError(name)


FAULTS = [
    ("extractor down", "retries_exhausted"),
    ("extractor past the timeout", "retries_exhausted"),
    ("service timed the source out", "timeout"),
    ("service has no parser for the format", "format_unavailable"),
    ("artifact over the bound", "resource_limit"),
    ("artifact not JSON", "invalid_artifact"),
    ("artifact out of contract", "invalid_artifact"),
    ("block under a missing parent", "invalid_artifact"),
    ("locator past the source", "invalid_artifact"),
    ("compressed answer", "retries_exhausted"),
    ("no extractor configured", "format_unavailable"),
    ("bucket fails the original's read", "retries_exhausted"),
    ("bucket refuses the artifact's write", "retries_exhausted"),
    ("embedder down", "retries_exhausted"),
    ("embedder past its deadline", "retries_exhausted"),
    ("embedder defect", "internal"),
    ("document row refused after the chunks", "internal"),
]


@pytest.mark.parametrize(("fault", "code"), FAULTS, ids=[f for f, _ in FAULTS])
def test_a_fault_on_the_next_revision_fails_typed_and_the_earlier_one_keeps_serving(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Any,
    fault: str,
    code: str,
) -> None:
    assert _run(world, _ingest(world, _upload(world))) == "ready"
    assert len(world.documents) == 1
    first = dict(world.documents[0])  # a copy: a change made to the row in place shows
    served = [(c["doc_version"], c["chunk_idx"]) for c in world.chunks]
    settings_override(knowledge_ingest_max_attempts=1)
    _fault(fault, world, monkeypatch, settings_override)
    job_id = _ingest(world, _upload(world, SQL + b"SELECT 2;\n"))
    assert _run(world, job_id) == "failed"
    job = world.job(job_id)
    assert job is not None and (job["state"], job["error_code"]) == ("failed", code)
    assert job["error_message"], "a failed job says why"
    # Revision 1 is the live row, and only its chunks are served.
    assert world.documents == [first]
    live = [
        (c["doc_version"], c["chunk_idx"])
        for c in world.chunks
        if c["doc_version"] not in world.dead
    ]
    assert live == served
