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
- A CSV publishes its rows as passages, a hit citing its rows, and its table as
  a typed dataset a writer approves (retyping a column) and a delete removes.
- An upload whose bytes contradict its name is stored, typed by its name, and its
  job fails with the extractor's code; an ingest of an unknown upload is 404.

The job sends originals to the extractor the settings route each format to; the
tests that publish skip unless a configured one declares SQL, TOML and PDF, and
the CSV test skips unless one declares CSV (`extractors.available_formats`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

import httpx
import pytest

from tests.fixtures.office_files import pdf
from tests.fixtures.xlsx_files import cell, sheet, workbook
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
        up = await _upload(c, org, ns, data, "retention.csv")
        assert up.status_code == 201, up.text
        job = await _op(
            c,
            "knowledge.ingest",
            org=org,
            namespace=ns,
            upload_id=up.json()["upload_id"],
            source_uri=uri,
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
    hit = next((h for h in hits["results"] if "rollups" in h["content"]), None)
    assert hit is not None, hits
    assert hit["locator"] == {"row_start": 2, "row_end": 3} and hit["format"] == "csv"
    assert "Table: rollups; Days: 365; Owner: analytics" in hit["content"]


async def _dataset_rows(org: str, ns: str, dataset_id: str) -> list[dict[str, Any]]:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    try:
        return await ch.query_all(
            "SELECT snapshot_id, row_id, s1, s2, n1, user_id, source_uri FROM loom.dataset_rows"
            " WHERE org = {org:String} AND namespace = {ns:String} AND dataset_id = {ds:String}"
            " ORDER BY snapshot_id, row_id",
            {"org": org, "ns": ns, "ds": dataset_id},
        )
    finally:
        await ch.disconnect()


async def _plant_running_rows(org: str, ns: str, uri: str, dataset_id: str) -> int:
    """A catalog row and one row of a dataset at a document version between the
    source's live one and now, as an ingest still running writes them; returns
    its snapshot id."""
    from server.core.ch import get_client
    from server.knowledge.store import datasets as datasets_repo

    ch = get_client()
    await ch.connect()
    try:
        (live,) = await datasets_repo.at_version(
            org, ns, uri, await _live_version(ch, org, ns, uri)
        )
        version = int(live["doc_version"]) + 1
        running = datasets_repo.dataset_row(live, doc_version=version, snapshot_id=version)
        await datasets_repo.insert_catalog([running], ch=ch)
        row = datasets_repo.slot_row(running, {"row_id": 0, "s1": "x"}, live["created_at"])
        await datasets_repo.insert_rows([row], ch=ch)
        return version
    finally:
        await ch.disconnect()


async def _live_version(ch: Any, org: str, ns: str, uri: str) -> int:
    row = await ch.query_one(
        "SELECT max(doc_version) AS v FROM loom.datasets FINAL WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String}",
        {"org": org, "ns": ns, "uri": uri},
    )
    return int(row["v"])


async def _deletes_running(since: Any) -> int:
    """The dataset tables' mutations (a lightweight delete is one) created at
    `since` or later and not yet applied: this test's, and any another run on
    the same server makes meanwhile."""
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    try:
        row = await ch.query_one(
            "SELECT count() AS n FROM system.mutations WHERE database = 'loom'"
            " AND table IN ('datasets', 'dataset_rows') AND is_done = 0"
            " AND create_time >= {t:DateTime}",
            {"t": since},
        )
        return int(row["n"]) if row else 0
    finally:
        await ch.disconnect()


async def _catalog_at(org: str, ns: str, uri: str, doc_version: int) -> int:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    try:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.datasets WHERE org = {org:String}"
            " AND namespace = {ns:String} AND source_uri = {uri:String}"
            " AND doc_version = {v:UInt64}",
            {"org": org, "ns": ns, "uri": uri, "v": doc_version},
        )
        return int(row["n"]) if row else 0
    finally:
        await ch.disconnect()


async def _catalog_rows(org: str, ns: str, uri: str) -> int:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    try:
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.datasets WHERE org = {org:String}"
            " AND namespace = {ns:String} AND source_uri = {uri:String}",
            {"org": org, "ns": ns, "uri": uri},
        )
        return int(row["n"]) if row else 0
    finally:
        await ch.disconnect()


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_a_csv_table_is_a_typed_dataset_a_writer_approves_and_a_delete_removes(
    control_plane_url: str,
) -> None:
    from server.knowledge.app.extractors import available_formats

    if "csv" not in await available_formats():
        pytest.skip("no configured document extractor declares csv")
    org, ns, uri = unique_org("ingest-ds"), "docs", "repo://budget.csv"
    data = b"team,amount\nplatform,120.5\nanalytics,n/a\nsearch,80\n"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        up = await _upload(c, org, ns, data, "budget.csv")
        assert up.status_code == 201, up.text
        job = await _op(
            c, "knowledge.ingest", org=org, namespace=ns, upload_id=up.json()["upload_id"],
            source_uri=uri,
        )  # fmt: skip
        await _work(org, ns, job["ingestion_id"])
        got = await _op(
            c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri, sample_rows=5
        )
        assert got["found"] and got["revision"] == 1
        (d,) = got["datasets"]
        assert d["schema_state"] == "needs_clarification" and d["row_count"] == 3
        assert [(c_["name"], c_["type"], c_["inferred"]) for c_ in d["columns"]] == [
            ("team", "string", "string"),
            ("amount", "string", "mixed"),
        ]
        assert [x["code"] for x in d["clarifications"]] == ["mixed_types"]
        assert d["rows"] == [
            {"team": "platform", "amount": "120.5"},
            {"team": "analytics", "amount": "n/a"},
            {"team": "search", "amount": "80"},
        ]
        stored = await _dataset_rows(org, ns, d["dataset_id"])
        assert [(r["row_id"], r["s1"], r["s2"]) for r in stored] == [
            (0, "platform", "120.5"),
            (1, "analytics", "n/a"),
            (2, "search", "80"),
        ]
        assert {(str(r["snapshot_id"]), r["source_uri"]) for r in stored} == {
            (d["snapshot_id"], uri)
        }

        stale = await c.post(
            "/v1/knowledge.dataset_approve",
            json={"org": org, "namespace": ns, "source_uri": uri,
                  "dataset_id": d["dataset_id"], "snapshot_id": "1"},
        )  # fmt: skip
        assert stale.status_code == 409, stale.text
        approved = await _op(
            c, "knowledge.dataset_approve", org=org, namespace=ns, source_uri=uri,
            dataset_id=d["dataset_id"], snapshot_id=d["snapshot_id"],
            columns={"amount": "number"},
        )  # fmt: skip
        assert approved["found"] and approved["retyped"]
        a = approved["dataset"]
        assert a["schema_state"] == "approved" and a["clarifications"] == []
        assert (a["row_count"], a["rejected_rows"]) == (2, 1)
        assert a["rejected"] == [{"row": 2, "column": "amount", "reason": "'n/a' is not a number"}]
        assert a["snapshot_id"] != d["snapshot_id"] and a["approved_at"]
        again = await _op(
            c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri, sample_rows=5
        )
        assert again["datasets"][0]["snapshot_id"] == a["snapshot_id"]
        # A metadata edit keeps serving it, approved.
        await _op(c, "knowledge.update_metadata", org=org, namespace=ns, source_uri=uri,
                  title="Budget 2026")  # fmt: skip
        edited = await _op(c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri)
        assert [(x["snapshot_id"], x["schema_state"]) for x in edited["datasets"]] == [
            (a["snapshot_id"], "approved")
        ]
        assert again["datasets"][0]["rows"] == [
            {"team": "platform", "amount": 120.5},
            {"team": "search", "amount": 80},
        ]

        await _op(c, "knowledge.delete", org=org, namespace=ns, source_uri=uri)
        gone = await _op(c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri)
        assert gone == {"found": False, "revision": 0, "datasets": []}
    # The delete does not wait for the rows to go: they go soon after.
    for _ in range(50):
        if not await _dataset_rows(org, ns, d["dataset_id"]):
            break
        await asyncio.sleep(0.2)
    assert await _dataset_rows(org, ns, d["dataset_id"]) == []
    assert await _catalog_rows(org, ns, uri) == 0


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


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_a_bulk_delete_removes_each_sources_datasets_at_its_own_version(
    control_plane_url: str,
) -> None:
    from server.knowledge.app.extractors import available_formats

    if "csv" not in await available_formats():
        pytest.skip("no configured document extractor declares csv")
    org, ns = unique_org("ingest-ds-bulk"), "docs"
    files = [("repo://a.csv", b"k,v\na,1\n"), ("repo://b.csv", b"k,v\nb,2\n"),
             ("repo://b.csv", b"k,v\nb,3\nc,4\n")]  # fmt: skip
    ids: dict[str, str] = {}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        for uri, data in files:  # b.csv twice: its live version is not a.csv's
            up = await _upload(c, org, ns, data, uri.removeprefix("repo://"))
            assert up.status_code == 201, up.text
            job = await _op(
                c, "knowledge.ingest", org=org, namespace=ns, upload_id=up.json()["upload_id"],
                source_uri=uri,
            )  # fmt: skip
            await _work(org, ns, job["ingestion_id"])
            got = await _op(c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri)
            ids[uri] = got["datasets"][0]["dataset_id"]
        assert got["revision"] == 2
        # An ingest of a.csv still running: rows of a version past a.csv's live one
        # and before b.csv's, which a bound of b.csv's version would take too.
        running = await _plant_running_rows(org, ns, "repo://a.csv", ids["repo://a.csv"])
        await _op(
            c, "knowledge.bulk_delete", org=org, namespace=ns,
            source_uris=["repo://a.csv", "repo://b.csv"],
        )  # fmt: skip
    for _ in range(50):  # until the delete's rows are gone; the running ingest's stay
        a_rows = await _dataset_rows(org, ns, ids["repo://a.csv"])
        b_rows = await _dataset_rows(org, ns, ids["repo://b.csv"])
        if not b_rows and [r["snapshot_id"] for r in a_rows] == [running]:
            break
        await asyncio.sleep(0.2)
    assert await _dataset_rows(org, ns, ids["repo://b.csv"]) == []
    assert await _catalog_rows(org, ns, "repo://b.csv") == 0
    left = await _dataset_rows(org, ns, ids["repo://a.csv"])
    assert [r["snapshot_id"] for r in left] == [running]  # the running ingest's, kept
    assert await _catalog_rows(org, ns, "repo://a.csv") == 1


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_a_delete_bounded_before_a_metadata_edit_keeps_the_carried_snapshots_rows(
    control_plane_url: str,
) -> None:
    """A metadata edit carries a snapshot to its new version while its rows keep
    the version they were written at; a delete that read the earlier version
    (and lost the race to the edit) must not take the rows the edit serves."""
    from server.core.ch import get_client
    from server.knowledge.app.extractors import available_formats
    from server.knowledge.store import datasets as datasets_repo

    if "csv" not in await available_formats():
        pytest.skip("no configured document extractor declares csv")
    org, ns, uri = unique_org("ingest-ds-edit"), "docs", "repo://edit.csv"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        up = await _upload(c, org, ns, b"k,v\na,1\nb,2\n", "edit.csv")
        assert up.status_code == 201, up.text
        job = await _op(
            c, "knowledge.ingest", org=org, namespace=ns, upload_id=up.json()["upload_id"],
            source_uri=uri,
        )  # fmt: skip
        await _work(org, ns, job["ingestion_id"])
        (d,) = (await _op(c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri))[
            "datasets"
        ]
        ch = get_client()
        await ch.connect()
        try:
            published = await _live_version(ch, org, ns, uri)
            await _op(c, "knowledge.update_metadata", org=org, namespace=ns, source_uri=uri,
                      title="Edited")  # fmt: skip
            since = (await ch.query_one("SELECT now() AS t"))["t"]  # the server's clock
            await datasets_repo.forget_published(org, ns, {uri: published}, ch=ch)
        finally:
            await ch.disconnect()
        # The delete does not wait: each table's delete is a mutation of its own,
        # so the rows are read once both have run.
        for _ in range(50):
            if not await _catalog_at(org, ns, uri, published) and not await _deletes_running(since):
                break
            await asyncio.sleep(0.2)
        assert not await _catalog_at(org, ns, uri, published)
        assert not await _deletes_running(since)
        got = await _op(c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri,
                        sample_rows=5)  # fmt: skip
    (served,) = got["datasets"]
    assert served["snapshot_id"] == d["snapshot_id"] and served["row_count"] == 2
    assert [r["k"] for r in served["rows"]] == ["a", "b"]


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_a_workbooks_sheets_are_datasets_each_sampled_past_its_rejected_rows(
    control_plane_url: str,
) -> None:
    """Two sheets, one with a cell error: one read samples both, each dataset
    cut to the rows asked for, the rejected row's id a gap it reads past."""
    from server.knowledge.app.extractors import available_formats

    if "xlsx" not in await available_formats():
        pytest.skip("no configured document extractor declares xlsx")
    org, ns, uri = unique_org("ingest-ds-xlsx"), "docs", "repo://book.xlsx"
    first = sheet({
        1: [cell("A1", "k", t="inlineStr"), cell("B1", "v", t="inlineStr")],
        2: [cell("A2", "a", t="inlineStr"), cell("B2", 1)],
        3: [cell("A3", "b", t="inlineStr"), cell("B3", "#N/A", t="e")],
        4: [cell("A4", "c", t="inlineStr"), cell("B4", 3)],
        5: [cell("A5", "d", t="inlineStr"), cell("B5", 4)],
    })  # fmt: skip
    second = sheet({
        1: [cell("A1", "k", t="inlineStr"), cell("B1", "v", t="inlineStr")],
        2: [cell("A2", "x", t="inlineStr"), cell("B2", 10)],
        3: [cell("A3", "y", t="inlineStr"), cell("B3", 20)],
        4: [cell("A4", "z", t="inlineStr"), cell("B4", 30)],
    })  # fmt: skip
    data = workbook([("One", first), ("Two", second)])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        up = await _upload(c, org, ns, data, "book.xlsx")
        assert up.status_code == 201, up.text
        job = await _op(
            c, "knowledge.ingest", org=org, namespace=ns, upload_id=up.json()["upload_id"],
            source_uri=uri,
        )  # fmt: skip
        await _work(org, ns, job["ingestion_id"])
        got = await _op(c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri,
                        sample_rows=2)  # fmt: skip
    one, two = got["datasets"]
    assert (one["sheet"], one["row_count"], one["rejected_rows"]) == ("One", 3, 1)
    assert one["rows"] == [{"k": "a", "v": 1}, {"k": "c", "v": 3}]
    assert (two["sheet"], two["row_count"]) == ("Two", 3)
    assert two["rows"] == [{"k": "x", "v": 10}, {"k": "y", "v": 20}]


@pytest.mark.usefixtures("extractor_routed")
@pytest.mark.asyncio
async def test_a_parquet_file_is_a_typed_dataset_its_decimals_exact(control_plane_url: str) -> None:
    import io
    from decimal import Decimal

    from server.knowledge.app.extractors import available_formats

    if "parquet" not in await available_formats():
        pytest.skip("no configured document extractor declares parquet")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    amounts = [Decimal("99999999999999999999.123456789"), Decimal("0.100000000")]
    buf = io.BytesIO()
    pq.write_table(
        pa.table(
            {"team": ["platform", "search"], "amount": pa.array(amounts, pa.decimal128(38, 9))}
        ),
        buf,
    )
    org, ns, uri = unique_org("ingest-pq"), "docs", "repo://budget.parquet"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        up = await _upload(c, org, ns, buf.getvalue(), "budget.parquet")
        assert up.status_code == 201, up.text
        assert up.json()["format"] == "parquet"
        job = await _op(
            c, "knowledge.ingest", org=org, namespace=ns, upload_id=up.json()["upload_id"],
            source_uri=uri,
        )  # fmt: skip
        await _work(org, ns, job["ingestion_id"])
        status = await _op(
            c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
        )
        assert (status["state"], status["format"]) == ("ready", "parquet")
        got = await _op(c, "knowledge.dataset_get", org=org, namespace=ns, source_uri=uri,
                        sample_rows=2)  # fmt: skip
        hits = await _op(c, "knowledge.search", org=org, namespace=ns, query="platform amount",
                         namespace_scope="this", search_mode="lexical")  # fmt: skip
    (d,) = got["datasets"]
    assert [(x["name"], x["type"]) for x in d["columns"]] == [
        ("team", "string"),
        ("amount", "number"),
    ]
    assert d["schema_state"] == "inferred" and d["row_count"] == 2
    # Past 2^53 and fractional, an exact decimal goes out as its text.
    assert d["rows"] == [
        {"team": "platform", "amount": "99999999999999999999.123456789"},
        {"team": "search", "amount": 0.1},
    ]
    hit = next(h for h in hits["results"] if "platform" in h["content"])
    assert hit["format"] == "parquet" and hit["locator"] == {"row_start": 1, "row_end": 2}
