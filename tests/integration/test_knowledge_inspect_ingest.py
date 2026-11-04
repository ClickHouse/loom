"""knowledge.inspect's ingestion and extraction sections against a live Loom + ClickHouse.

Each source is ingested through the wire (knowledge.upload, knowledge.ingest) and its job
run in this process as a knowledge_ingest worker pass runs one (the server under test runs
no workers), so each job reaches its state the way production reaches it:

- backlog: a job queued and not yet run (and a job cancelled while pending, which is not);
- extractor unavailable: a PDF no configured extractor takes (failed `format_unavailable`;
  another such PDF that a text write then replaced is in no finding), and a SQL file whose
  extractor is stopped (an unreachable URL: the job is back to pending, retrying);
- missing access: an original no longer stored (`original_gone`);
- with the reference extractor at LOOM_DOCUMENT_EXTRACTOR_URL, also missing access (the
  extractor refuses the server's token), bad content (a TOML file it cannot parse) and a
  partial SQL extraction published with allow_partial that lost its structure.

The report keeps each apart with its `kind`, names each job's id, and writes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

SQL = b"CREATE TABLE t (id UInt64) ENGINE = MergeTree ORDER BY id;\n\nSELECT 1;\n"
PARTIAL_SQL = b"SELECT 1;\n\nSELEC ((( broken garbage ;\n\nSELECT 2;\n"
BAD_TOML = b"a = = 1\n[x\n"


async def _work(job_id: str, c: httpx.AsyncClient, org: str, ns: str) -> dict[str, Any]:
    """Run the job once in this process, as a worker pass that took it does (a pass
    takes one job of the whole queue, which other tests' jobs share), then its status."""
    from server.core.ch import get_client
    from server.knowledge.app import ingest_run
    from server.knowledge.store import ingestions

    ch = get_client()
    await ch.connect()
    try:
        job = await ingestions.get_ingestion(org, ns, job_id)
        assert job is not None, job_id
        assert await ingest_run.take_and_run(job) is not None, f"job {job_id} did not run"
    finally:
        await ch.disconnect()
    return await _op(c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job_id)


async def _end(c: httpx.AsyncClient, org: str, ns: str, job_ids: list[str]) -> None:
    """Cancel the jobs left open and run each, which ends it, so no open job of this
    namespace is left on the shared queue (a retried one included: a run does not wait
    for its next attempt)."""
    from server.core.ch import get_client
    from server.knowledge.app import ingest_run
    from server.knowledge.store import ingestions

    for job_id in job_ids:
        await _op(c, "knowledge.ingest_cancel", org=org, namespace=ns, ingestion_id=job_id)
    ch = get_client()
    await ch.connect()
    try:
        for job_id in job_ids:
            job = await ingestions.get_ingestion(org, ns, job_id)
            assert job is not None, job_id
            await ingest_run.take_and_run(job)
        # A cancelled pending job's row stays pending until a run ends it.
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.knowledge_ingestions FINAL WHERE org = {org:String}"
            " AND namespace = {ns:String} AND state IN ('pending', 'processing')",
            params={"org": org, "ns": ns},
        )
    finally:
        await ch.disconnect()
    assert row is not None and not int(row["n"]), "the jobs left open did not end"


async def _op(c: httpx.AsyncClient, op: str, **body: Any) -> dict[str, Any]:
    """The op's answer; a call the namespace's rate limit refuses waits and is sent again."""
    for _ in range(10):
        r = await c.post(f"/v1/{op}", json=body)
        if r.status_code != 429:
            break
        await asyncio.sleep(float(r.json()["detail"].get("retry_after_s") or 1))
    assert r.status_code == 200, (op, r.text[:400])
    return dict(r.json())


async def _ingest(
    c: httpx.AsyncClient, org: str, ns: str, data: bytes, filename: str, **body: Any
) -> tuple[str, str]:
    """Upload `data` and ingest it at `repo://<filename>`: (source_uri, ingestion_id)."""
    sha = hashlib.sha256(data).hexdigest()
    for _ in range(10):
        up = await c.post(
            "/v1/knowledge.upload",
            params={"org": org, "namespace": ns, "filename": filename, "sha256": sha},
            content=data,
        )
        if up.status_code != 429:
            break
        await asyncio.sleep(float(up.json()["detail"].get("retry_after_s") or 1))
    assert up.status_code == 201, up.text
    uri = f"repo://{filename}"
    job = await _op(
        c,
        "knowledge.ingest",
        org=org,
        namespace=ns,
        upload_id=up.json()["upload_id"],
        source_uri=uri,
        **body,
    )
    return uri, job["ingestion_id"]


def _findings(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["code"]: f for f in out["ingest_findings"]}


@pytest.mark.asyncio
async def test_backlog_a_missing_extractor_and_missing_access_are_kept_apart(
    control_plane_url: str, ch_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    org, ns = unique_org("kinspect-ingest"), f"ki-ingest-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        # No configured extractor takes pdf: the job fails for the deployment.
        pdf, pdf_job = await _ingest(c, org, ns, b"plain words, not a PDF", "report.pdf")
        assert (await _work(pdf_job, c, org, ns))["error_code"] == "format_unavailable"
        # The same failure, then a text write of the source: the write replaced the job.
        fixed, fixed_job = await _ingest(c, org, ns, b"also not a PDF", "fixed.pdf")
        assert (await _work(fixed_job, c, org, ns))["error_code"] == "format_unavailable"
        await asyncio.sleep(1.1)  # the document row's ingested_at counts seconds
        await _op(c, "knowledge.add", org=org, namespace=ns, source_uri=fixed, content="Fixed.")

        # The original is no longer stored when the job runs.
        gone_bytes = b"# Gone\nThis original is removed.\n"
        gone, gone_job = await _ingest(c, org, ns, gone_bytes, "gone.md")
        ch_client.command(
            "DELETE FROM loom.document_blobs WHERE org = {org:String} AND namespace = {ns:String}"
            " AND sha256 = {sha:String}",
            parameters={"org": org, "ns": ns, "sha": hashlib.sha256(gone_bytes).hexdigest()},
        )
        assert (await _work(gone_job, c, org, ns))["error_code"] == "original_gone"

        # The extractor is stopped: the job goes back to pending and retries.
        with monkeypatch.context() as m:
            m.setattr(settings, "document_extractor_url", "http://127.0.0.1:9")
            m.setattr(settings, "document_extractors_json", "{}")
            down, down_job = await _ingest(c, org, ns, SQL, "down.sql")
            status = await _work(down_job, c, org, ns)
        assert (status["state"], status["error_code"]) == ("pending", "retrying")

        # Never run: the backlog.
        queued, queued_job = await _ingest(c, org, ns, SQL, "queued.sql")
        # Cancelled while pending: its row stays pending until a worker takes it.
        cancelled, cancelled_job = await _ingest(c, org, ns, SQL, "cancelled.sql")
        await _op(c, "knowledge.ingest_cancel", org=org, namespace=ns, ingestion_id=cancelled_job)

        before = ch_client.query(
            "SELECT count(), max(version) FROM loom.knowledge_ingestions WHERE org = {org:String}",
            parameters={"org": org},
        ).result_rows
        out = await _op(c, "knowledge.inspect", org=org, namespace=ns, sample_limit=10)
        after = ch_client.query(
            "SELECT count(), max(version) FROM loom.knowledge_ingestions WHERE org = {org:String}",
            parameters={"org": org},
        ).result_rows
        await _end(c, org, ns, [queued_job, down_job, cancelled_job])
    assert after == before
    assert out["skipped"] == [], out["skipped"]

    i = out["ingestions"]
    assert (i["sources"], i["without_document"], i["replaced"]) == (6, 5, 1)
    # The cancelled job counts as cancelled, as knowledge.ingest_status answers it.
    assert (i["pending"], i["failed"], i["cancelled"], i["processing"], i["stalled"]) == (
        2,
        3,
        1,
        0,
        0,
    )
    assert i["oldest_open_queued_at"] is not None
    # The server's own settings: workers off here, an extractor configured or not.
    assert i["worker_enabled"] == settings.knowledge_ingest_enabled
    assert {s["source_uri"] for s in i["without_document_listed"]} == {
        pdf,
        gone,
        down,
        queued,
        cancelled,
    }

    f = _findings(out)
    backlog = f["ingest_backlog"]
    assert (backlog["kind"], backlog["count"], backlog["without_document"]) == (
        "system_state",
        1,
        1,
    )
    assert (backlog["samples"], backlog["ingestion_ids"]) == ([queued], [queued_job])
    unavailable = f["extractor_unavailable"]
    assert unavailable["kind"] == "system_state"
    assert dict(zip(unavailable["samples"], unavailable["ingestion_ids"], strict=True)) == {
        pdf: pdf_job,
        down: down_job,
    }
    access = f["ingest_missing_access"]
    assert (access["kind"], access["samples"], access["ingestion_ids"]) == (
        "system_state",
        [gone],
        [gone_job],
    )
    assert f["ingest_content_rejected"]["count"] == 0
    assert f["extraction_incomplete"]["count"] == 0
    assert out["coverage"]["complete"] is True

    # Each source's open job is named, so knowledge.ingest_status can be called with it.
    listed = {s["source_uri"]: s for s in i["without_document_listed"]}
    assert listed[down]["open_ingestion_id"] == down_job
    assert listed[down]["current"]["failure_class"] == "extractor_unavailable"
    assert listed[down]["current"]["next_attempt_at"] is not None
    assert listed[pdf]["open_ingestion_id"] == "" and listed[pdf]["current"]["ended_at"]
    assert listed[gone]["current"]["failure_class"] == "missing_access"
    c_job = listed[cancelled]
    assert (c_job["open_ingestion_id"], c_job["current"]["state"]) == ("", "cancelled")
    assert c_job["current"]["failure_class"] == "cancelled" and c_job["current"]["ended_at"]
    assert f["ingest_failed_on_server"]["count"] == 0
    # The replaced source is in no finding.
    assert all(fixed not in g["samples"] for g in out["ingest_findings"])


@pytest.mark.asyncio
async def test_with_the_extractor_bad_content_lost_structure_and_a_refused_token(
    control_plane_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    if not settings.document_extractor_url:
        pytest.skip("needs the reference extractor at LOOM_DOCUMENT_EXTRACTOR_URL")
    org, ns = unique_org("kinspect-extract"), f"ki-extract-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        # A published revision, then a later file of the same source the extractor cannot
        # parse: the job fails and the earlier revision keeps serving.
        conf = "repo://conf.toml"
        _, first = await _ingest(c, org, ns, b"[server]\nport = 1\n", "conf.toml")
        assert (await _work(first, c, org, ns))["state"] == "ready"
        _, bad_job = await _ingest(c, org, ns, BAD_TOML, "conf.toml")
        status = await _work(bad_job, c, org, ns)
        assert (status["state"], status["error_code"]) == ("failed", "incomplete_extraction")

        # Published with what was extracted: one statement lost its structure.
        partial, partial_job = await _ingest(
            c, org, ns, PARTIAL_SQL, "partial.sql", allow_partial=True
        )
        assert (await _work(partial_job, c, org, ns))["state"] == "partial"

        # The extractor refuses the server's token; one attempt, so it fails for good.
        with monkeypatch.context() as m:
            m.setattr(settings, "document_extractor_token", "not-the-extractors-token")
            m.setattr(settings, "document_extractor_tokens_json", "{}")
            m.setattr(settings, "knowledge_ingest_max_attempts", 1)
            token, token_job = await _ingest(c, org, ns, SQL, "token.sql")
            status = await _work(token_job, c, org, ns)
        assert (status["state"], status["error_code"]) == ("failed", "retries_exhausted")

        out = await _op(c, "knowledge.inspect", org=org, namespace=ns, sample_limit=10)
    assert out["skipped"] == [], out["skipped"]
    assert out["ingestions"]["extractor_configured"] is True, (
        "the server has no document extractor configured: give it the test's settings"
    )

    f = _findings(out)
    rejected = f["ingest_content_rejected"]
    assert (rejected["kind"], rejected["samples"], rejected["ingestion_ids"]) == (
        "contract_violation",
        [conf],
        [bad_job],
    )
    # The source has a live document: its earlier revision.
    assert rejected["without_document"] == 0
    incomplete = f["extraction_incomplete"]
    assert (incomplete["kind"], incomplete["samples"], incomplete["population"]) == (
        "usage_heuristic",
        [partial],
        2,
    )
    access = f["ingest_missing_access"]
    assert (access["samples"], access["ingestion_ids"]) == ([token], [token_job])
    assert f["ingest_backlog"]["count"] == 0 and f["extractor_unavailable"]["count"] == 0
    assert {c["name"]: c["live"] for c in out["extraction"]["lost"]} == {"sql_structure": 1}
    assert (out["extraction"]["documents"], out["extraction"]["incomplete"]) == (2, 1)

    listed = {d["source_uri"]: d for d in out["listed"]}
    p = listed[partial]
    assert p["extraction"]["unit"] == "statements" and p["extraction"]["lost"] == ["sql_structure"]
    assert (p["extraction"]["total"], p["extraction"]["extracted"], p["extraction"]["failed"]) == (
        3,
        2,
        1,
    )
    assert p["extraction"]["complete"] is False
    assert p["jobs"]["latest"]["state"] == "partial" and p["jobs"]["open_jobs"] == 0
    cf = listed[conf]
    assert cf["extraction"]["complete"] is True and cf["ingestion_id"] == first
    # The listed document names the later job that failed while its revision stayed live.
    assert (cf["jobs"]["jobs"], cf["jobs"]["latest"]["ingestion_id"]) == (2, bad_job)
    assert cf["jobs"]["latest"]["failure_class"] == "bad_content"
