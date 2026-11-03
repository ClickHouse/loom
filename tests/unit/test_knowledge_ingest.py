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
from typing import Any

import pytest

from server.core.errors import (
    BackendTimeoutError,
    BackendUnavailableError,
    NotFoundError,
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
    cancels: list[str] = field(default_factory=list)  # ingestion ids with a cancel
    fail_document: Exception | None = None
    late_documents: list[dict[str, Any]] = field(default_factory=list)  # timed out, land later
    claim_busy: bool = False  # another write holds the source's claim
    fail_retire_older: Exception | None = None  # raised once, by the next older-chunks retire
    log: list[str] = field(default_factory=list)
    stored_vectors: dict[str, list[float]] = field(default_factory=dict)
    embedded: list[str] = field(default_factory=list)
    fail_embed: Exception | None = None

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
        w.chunks.extend(rows)
        w.log.append("chunks")
        return len(rows)

    async def insert_one(row: dict[str, Any], **_: Any) -> None:
        if w.fail_document is not None:
            w.late_documents.append(dict(row))
            raise w.fail_document
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
            w.log.append("retire")
            return
        if w.fail_retire_older is not None:
            e, w.fail_retire_older = w.fail_retire_older, None
            raise e
        w.superseded.append((uri, before_version or 0))
        w.log.append("supersede")

    async def refuse_width(*a: Any, **k: Any) -> None:
        return None

    class _Lease:
        held = True

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
    async def holding(*a: Any, **k: Any) -> Any:
        yield not w.claim_busy

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
        return sum(1 for c in w.chunks if c["source_uri"] == uri)

    monkeypatch.setattr(chunks_repo, "count_active_for_doc", count_active_for_doc)

    async def count_version(org: str, ns: str, uri: str, version: int, **_: Any) -> int:
        if version in w.retired:
            return 0
        return sum(1 for c in w.chunks if c["source_uri"] == uri and c["doc_version"] == version)

    monkeypatch.setattr(chunks_repo, "count_version", count_version)
    monkeypatch.setattr(chunks_repo, "mark_doc_superseded", mark_doc_superseded)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    monkeypatch.setattr(documents_repo, "get_one", get_one)

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
        (b"%PDF-1.7 ...", "a.pdf", {}, "format_unavailable"),
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


@pytest.mark.parametrize("attempts", [1, 5])
def test_a_failure_after_the_document_row_ends_the_job_published(
    world: World, settings_override: Any, attempts: int
) -> None:
    """The document row landed and the retire of the earlier revision's chunks
    failed: on any attempt, last or not, the job ends published (the retire run
    again), not failed or back to pending."""
    settings_override(knowledge_ingest_max_attempts=attempts)
    _run(world, _ingest(world, _upload(world)))
    world.fail_retire_older = BackendUnavailableError("store down")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None and (job["revision"], job["error_code"]) == (2, "")
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
    """An edit writes a new document version and a delete a tombstone, both
    naming the job that published: the rerun knows its own publish, retires
    none of its chunks and brings nothing back."""
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None
    doc = world.documents[-1]
    edit = {"version": doc["version"] + 1}
    if after == "a delete":
        edit["superseded_at"] = _now()
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
    """A text write names no job. While the job's own version row is still
    there the rerun finds it and ends published with its own chunk count; once
    a merge has folded it into the text write's, the rerun cannot tell, and a
    later write landed after its chunks, so it fails `revision_conflict`. In
    neither case does it publish the older file again."""
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None
    own_chunks = int(job["chunks"])
    doc = world.documents[-1]
    world.documents.append(
        {**doc, "version": doc["version"] + 1, "revision": 2, "ingestion_id": ""}
    )
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
        assert (world.job(job_id)["revision"], world.job(job_id)["chunks"]) == (1, own_chunks)  # type: ignore[index]
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
    world.fail_document = BackendUnavailableError("store down")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "pending"
    doc = world.documents[-1]
    world.documents.append({**doc, "title": "Edited", "version": time.time_ns()})
    world.fail_document = None
    assert _run(world, job_id) == "ready"
    assert world.job(job_id)["revision"] == 2  # type: ignore[index]


def test_a_document_insert_that_timed_out_and_landed_later_ends_the_job_published(
    world: World, settings_override: Any
) -> None:
    """A client-side timeout does not stop the insert, so its chunks are not
    retired and the job goes back to pending even on its last attempt. When the
    row has landed the next run finishes from the job's own chunk version: the
    earlier revision's chunks retired, its own live and counted. When it has
    not, the next run retires them and the job fails."""
    settings_override(knowledge_ingest_max_attempts=1)
    _run(world, _ingest(world, _upload(world)))
    world.fail_document = BackendTimeoutError("timed out")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "pending" and world.retired == []
    version = world.job(job_id)["doc_version"]  # type: ignore[index]
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
    assert _run(world, lost) == "pending"
    world.fail_document = None  # the insert never landed
    assert _run(world, lost) == "failed"
    assert world.job(lost)["error_code"] == "retries_exhausted"  # type: ignore[index]
    assert world.retired == [world.job(lost)["doc_version"]]  # type: ignore[index]


def test_a_row_that_landed_after_its_chunks_were_retired_is_published_again(world: World) -> None:
    """Its own row with no live chunks of its own is not finished: the run
    publishes again at a fresh version over it, at the same revision."""
    _run(world, _ingest(world, _upload(world)))
    world.fail_document = BackendTimeoutError("timed out")
    job_id = _ingest(world, _upload(world))
    assert _run(world, job_id) == "pending"
    stale = world.job(job_id)["doc_version"]  # type: ignore[index]
    world.fail_document = None
    world.retired.append(stale)  # retired before the row landed
    world.documents.extend(world.late_documents)
    assert _run(world, job_id) == "ready"
    job = world.job(job_id)
    assert job is not None and job["revision"] == 2 and job["doc_version"] != stale
    latest = world.documents[-1]
    assert (latest["ingestion_id"], latest["revision"], latest["version"]) == (
        job_id,
        2,
        job["doc_version"],
    )
