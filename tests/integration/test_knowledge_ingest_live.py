"""Server-side ingestion end to end, against a running server and its ClickHouse:
a file is uploaded over HTTP, ingested with knowledge.ingest, and published by a
pass of the knowledge_ingest worker run in this process (the server under test
runs no workers); then status, get, outline, search and download read it back.

- The SQL file publishes revision 1 with its format, parser and coverage, chunks
  that cite their statements' lines and carry their names as keys, and the
  original downloads byte for byte.
- A second ingest of the source is revision 2; one that expects revision 0
  fails `revision_conflict` and leaves revision 2 serving.
- An upload whose bytes contradict its name is stored, typed by its name, and its
  job fails with the extractor's code; an ingest of an unknown upload is 404.

The worker pass sends originals to the extractor the settings route SQL and TOML to
(`extractors.endpoint_for`); the tests that publish skip without one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture
def extractor_routed() -> None:
    """The tests that publish send SQL and TOML originals to an extractor; without one
    routed for them every job fails `format_unavailable`, which only the
    mislabelled-upload test expects. Asked as the worker asks (`endpoint_for`), from
    the settings: the environment, `.env` or the settings file."""
    from server.knowledge.app.extractors import endpoint_for

    if not (endpoint_for("sql", "") and endpoint_for("toml", "")):
        pytest.skip("no document extractor is routed for sql and toml")


SQL = (
    b"-- Raw events are kept for 30 days.\n"
    b"CREATE TABLE events (id UInt64, ts DateTime) ENGINE = MergeTree ORDER BY id\n"
    b"TTL ts + INTERVAL 30 DAY;\n\n"
    b"SELECT count() FROM events;\n"
)


async def _upload(
    c: httpx.AsyncClient, org: str, ns: str, data: bytes, filename: str
) -> httpx.Response:
    return await c.post(
        "/v1/knowledge.upload",
        params={
            "org": org,
            "namespace": ns,
            "filename": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )


async def _op(c: httpx.AsyncClient, op: str, **body: Any) -> dict[str, Any]:
    r = await c.post(f"/v1/{op}", json=body)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _work() -> None:
    """One worker pass in this process, on the same ClickHouse."""
    from server.core.ch import get_client
    from server.knowledge.workers import ingest

    ch = get_client()
    await ch.connect()
    try:
        await ingest.run_pass()
    finally:
        await ch.disconnect()


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_an_uploaded_sql_file_is_parsed_published_and_cited(control_plane_url: str) -> None:
    org, ns, uri = unique_org("ingest"), "docs", f"repo://retention-{uuid.uuid4().hex[:6]}.sql"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        up = await _upload(c, org, ns, SQL, "retention.sql")
        assert up.status_code == 201, up.text
        upload = up.json()
        assert upload["format"] == "sql" and upload["size"] == len(SQL)

        job = await _op(
            c,
            "knowledge.ingest",
            org=org,
            namespace=ns,
            upload_id=upload["upload_id"],
            source_uri=uri,
            title="Retention",
            options={"sql_dialect": "clickhouse"},
        )
        assert job["state"] == "pending"
        await _work()

        status = await _op(
            c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
        )
        assert (status["state"], status["retrieval"], status["revision"], status["format"]) == (
            "ready",
            "ready",
            1,
            "sql",
        )
        assert status["coverage"]["complete"] is True and status["chunks"] == 2

        doc = await _op(c, "knowledge.get", org=org, namespace=ns, source_uri=uri)
        assert (doc["revision"], doc["format"]) == (1, "sql") and doc["parser"].startswith(
            "loom-sql/"
        )
        assert "CREATE TABLE events" in doc["content"]

        hits = await _op(
            c,
            "knowledge.search",
            org=org,
            namespace=ns,
            query="how long are raw events kept",
            namespace_scope="this",
            search_mode="lexical",
        )
        create = next(h for h in hits["results"] if "CREATE TABLE" in h["content"])
        assert create["source_uri"] == uri

        r = await c.get(
            "/v1/knowledge.download", params={"org": org, "namespace": ns, "source_uri": uri}
        )
        assert r.status_code == 200 and r.content == SQL


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_revisions_count_up_and_an_expected_revision_guards_the_publish(
    control_plane_url: str, ch_client: Any
) -> None:
    org, ns, uri = unique_org("ingest-rev"), "docs", "repo://config.toml"
    toml = b"# Service limits.\n[server]\nport = 7777\n"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        for _ in range(2):
            up = (await _upload(c, org, ns, toml, "config.toml")).json()
            await _op(
                c,
                "knowledge.ingest",
                org=org,
                namespace=ns,
                upload_id=up["upload_id"],
                source_uri=uri,
            )
            await _work()
        doc = await _op(c, "knowledge.get", org=org, namespace=ns, source_uri=uri)
        assert doc["revision"] == 2 and doc["format"] == "toml"

        up = (await _upload(c, org, ns, toml, "config.toml")).json()
        stale = await _op(
            c,
            "knowledge.ingest",
            org=org,
            namespace=ns,
            upload_id=up["upload_id"],
            source_uri=uri,
            expected_revision=0,
        )
        await _work()
        status = await _op(
            c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=stale["ingestion_id"]
        )
        assert (status["state"], status["error_code"]) == ("failed", "revision_conflict")
        assert (await _op(c, "knowledge.get", org=org, namespace=ns, source_uri=uri))[
            "revision"
        ] == 2

    rows = ch_client.query(
        "SELECT chunk_metadata, keys FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String}"
        " AND source_uri = {uri:String} AND doc_superseded_at IS NULL AND (org, namespace, embedding_model,"
        " source_uri, doc_version) NOT IN (SELECT org, namespace, embedding_model, source_uri, doc_version"
        " FROM loom.chunk_supersessions)",
        parameters={"org": org, "ns": ns, "uri": uri},
    ).result_rows
    assert len(rows) == 1, "the earlier revision's chunks are retired"
    meta, keys = json.loads(rows[0][0]), list(rows[0][1])
    assert meta["locator"]["line_start"] == 1 and "server.port" in keys


@pytest.mark.asyncio
async def test_a_mislabelled_upload_is_stored_and_the_extractor_refuses_it(
    control_plane_url: str,
) -> None:
    """The server types an upload by its name and never reads the bytes: plain
    text named .pdf is a pdf upload, and the job fails with the extractor's code
    (no configured extractor takes pdf), publishing nothing."""
    org, ns = unique_org("ingest-refuse"), "docs"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        up = await _upload(c, org, ns, b"plain words, not a PDF", "report.pdf")
        assert up.status_code == 201 and up.json()["format"] == "pdf", up.text
        job = await _op(
            c,
            "knowledge.ingest",
            org=org,
            namespace=ns,
            upload_id=up.json()["upload_id"],
            source_uri="repo://report.pdf",
        )
        await _work()
        status = await _op(
            c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
        )
        assert (status["state"], status["error_code"]) == ("failed", "format_unavailable")
        r = await c.post(
            "/v1/knowledge.get",
            json={"org": org, "namespace": ns, "source_uri": "repo://report.pdf"},
        )
        assert r.status_code == 200 and r.json().get("found") is False, r.text


@pytest.mark.asyncio
async def test_an_ingest_of_an_unknown_upload_is_not_found(control_plane_url: str) -> None:
    org = unique_org("ingest-unknown")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        r = await c.post(
            "/v1/knowledge.ingest",
            json={
                "org": org,
                "namespace": "docs",
                "upload_id": str(uuid.uuid4()),
                "source_uri": "x",
            },
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_cancelled_pending_job_reads_cancelled_and_the_worker_ends_it(
    control_plane_url: str,
) -> None:
    """The cancel is a row of its own: the job reads cancelled at once, and the
    worker ends it without extracting or publishing."""
    org, ns, uri = unique_org("ingest-cancel"), "docs", "repo://cancelled.sql"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        up = (await _upload(c, org, ns, SQL, "cancelled.sql")).json()
        job = await _op(
            c, "knowledge.ingest", org=org, namespace=ns, upload_id=up["upload_id"], source_uri=uri
        )
        out = await _op(
            c, "knowledge.ingest_cancel", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
        )
        assert (out["state"], out["cancelled"]) == ("cancelled", True)
        status = await _op(
            c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
        )
        assert (status["state"], status["cancel_requested"]) == ("cancelled", True)
        await _work()
        status = await _op(
            c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
        )
        assert (status["state"], status["error_code"], status["revision"]) == (
            "cancelled",
            "cancelled",
            0,
        )
        r = await c.post("/v1/knowledge.get", json={"org": org, "namespace": ns, "source_uri": uri})
        assert r.status_code == 200 and r.json().get("found") is False, r.text
