"""Server-side ingestion end to end, against a running server and its ClickHouse:
a file is uploaded over HTTP, ingested with knowledge.ingest, and its job run in
this process as a knowledge_ingest worker pass runs one (the server under test
runs no workers); then status, get, outline, search and download read it back.

- The SQL file publishes revision 1 with its format, parser and coverage, chunks
  that cite their statements' lines and carry their names as keys, and the
  original downloads byte for byte.
- A second ingest of the source is revision 2; one that expects revision 0
  fails `revision_conflict` and leaves revision 2 serving.
- A PDF with a page of one image fails `incomplete_extraction`; with
  `allow_partial` it publishes as `partial`, its chunks citing their pages.
- A CSV publishes its rows as passages, a hit citing its rows.
- An upload whose bytes contradict its name is stored, typed by its name, and its
  job fails with the extractor's code; an ingest of an unknown upload is 404.

The job sends originals to the extractor the settings route each format to; the
tests that publish skip unless a configured one declares SQL, TOML and PDF, and
the CSV one CSV too (`extractors.available_formats`).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx
import pytest

from tests.fixtures.office_files import pdf
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture
async def extractor_routed() -> None:
    """Skips unless a configured extractor declares SQL, TOML and PDF, as the
    worker asks before it sends an original."""
    from server.knowledge.app.extractors import available_formats

    declared = await available_formats()
    if not all(fmt in declared for fmt in ("sql", "toml", "pdf")):
        pytest.skip("no configured document extractor declares sql, toml and pdf")


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


async def _work(org: str, ns: str, ingestion_id: str) -> None:
    """Run the job in this process, on the same ClickHouse, as a worker pass that
    took it does. A pass takes one job of the whole queue, which other tests'
    jobs share, so it may not be this one."""
    from server.core.ch import get_client
    from server.knowledge.app import ingest_run
    from server.knowledge.store import ingestions

    ch = get_client()
    await ch.connect()
    try:
        job = await ingestions.get_ingestion(org, ns, ingestion_id)
        assert job is not None, ingestion_id
        assert await ingest_run.take_and_run(job) is not None, f"job {ingestion_id} did not run"
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
        await _work(org, ns, job["ingestion_id"])

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
            job = await _op(
                c,
                "knowledge.ingest",
                org=org,
                namespace=ns,
                upload_id=up["upload_id"],
                source_uri=uri,
            )
            await _work(org, ns, job["ingestion_id"])
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
        await _work(org, ns, stale["ingestion_id"])
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


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_a_pdf_with_an_image_page_publishes_only_when_partial_is_allowed(
    control_plane_url: str, ch_client: Any
) -> None:
    org, ns, uri = unique_org("ingest-pdf"), "docs", "repo://handbook.pdf"
    data = pdf(["Escalation\nPage the on-call engineer.", None], [("Escalation", 0, [])])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        states = []
        for allow_partial in (False, True):
            up = (await _upload(c, org, ns, data, "handbook.pdf")).json()
            job = await _op(
                c,
                "knowledge.ingest",
                org=org,
                namespace=ns,
                upload_id=up["upload_id"],
                source_uri=uri,
                allow_partial=allow_partial,
            )
            await _work(org, ns, job["ingestion_id"])
            status = await _op(
                c,
                "knowledge.ingest_status",
                org=org,
                namespace=ns,
                ingestion_id=job["ingestion_id"],
            )
            states.append((status["state"], status["error_code"]))
        assert states == [("failed", "incomplete_extraction"), ("partial", "")]
        assert status["coverage"]["unit"] == "pages"
        assert [f["unit"] for f in status["coverage"]["failed"]] == ["page 2"]
        doc = await _op(c, "knowledge.get", org=org, namespace=ns, source_uri=uri)
        assert (doc["revision"], doc["format"]) == (1, "pdf")

    rows = ch_client.query(
        "SELECT chunk_metadata FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String}"
        " AND source_uri = {uri:String}",
        parameters={"org": org, "ns": ns, "uri": uri},
    ).result_rows
    metas = [json.loads(r[0]) for r in rows]
    assert [(m["locator"], m["section_path"]) for m in metas] == [
        ({"page_start": 1, "page_end": 1}, ["Escalation"])
    ]


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_a_csv_publishes_its_rows_as_passages_cited_by_row(control_plane_url: str) -> None:
    from server.knowledge.app.extractors import available_formats

    if "csv" not in await available_formats():
        pytest.skip("no configured document extractor declares csv")
    org, ns, uri = unique_org("ingest-csv"), "docs", "repo://retention.csv"
    data = b"Table,Days,Owner\nevents,30,platform\nrollups,365,analytics\n"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        up = (await _upload(c, org, ns, data, "retention.csv")).json()
        job = await _op(
            c, "knowledge.ingest", org=org, namespace=ns, upload_id=up["upload_id"], source_uri=uri
        )
        await _work(org, ns, job["ingestion_id"])
        status = await _op(
            c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
        )
        assert (status["state"], status["format"], status["coverage"]["unit"]) == (
            "ready",
            "csv",
            "rows",
        )
        hits = await _op(
            c,
            "knowledge.search",
            org=org,
            namespace=ns,
            query="how long are rollups kept",
            namespace_scope="this",
            search_mode="lexical",
        )
    hit = next(h for h in hits["results"] if "rollups" in h["content"])
    assert hit["locator"] == {"row_start": 2, "row_end": 3} and hit["format"] == "csv"
    assert "Table: rollups; Days: 365; Owner: analytics" in hit["content"]


@pytest.mark.asyncio
async def test_a_mislabelled_upload_is_stored_and_the_extractor_refuses_it(
    control_plane_url: str,
) -> None:
    """The server types an upload by its name and never reads the bytes: plain
    text named .pdf is a pdf upload, and the job fails with the extractor's code
    (the extractor finds no PDF in it; with none that declares pdf, the format
    is unavailable), publishing nothing."""
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
        await _work(org, ns, job["ingestion_id"])
        status = await _op(
            c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
        )
        from server.knowledge.app.extractors import available_formats

        declared = await available_formats()
        refused = "format_mismatch" if "pdf" in declared else "format_unavailable"
        assert (status["state"], status["error_code"]) == ("failed", refused)
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
        await _work(org, ns, job["ingestion_id"])
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
