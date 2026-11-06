"""namespace.tune's document drafts against a live Loom + ClickHouse.

Six text documents whose sections are longer than the chunk size, and a Parquet file
ingested through the wire whose job runs in this process with no extractor configured, as a
knowledge_ingest worker pass runs one, so it fails `format_unavailable` as production fails
it on a deployment with no extractor for Parquet.
Tune cuts the documents again in process and reports the cut: every section is cut, and
no larger size is drafted because the test embedder states no input limit, which
`skipped` says. The Parquet file is named in `skipped` with no route drafted: as a source to
upload again when the server's extractor declares Parquet, else as one no configured
extractor declares. Decision extraction is said to take no instructions, the same sample
comes back on a second call, and nothing is written: the document rows and chunks are as
they were.
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

SECTION = "word " * 800  # 4,000 characters, more than the default chunk size


async def _op(c: httpx.AsyncClient, op: str, **body: Any) -> dict[str, Any]:
    """The op's answer; a call the namespace's rate limit refuses waits and is sent again."""
    for _ in range(10):
        r = await c.post(f"/v1/{op}", json=body)
        if r.status_code != 429:
            break
        await asyncio.sleep(float(r.json()["detail"].get("retry_after_s") or 1))
    assert r.status_code == 200, (op, r.text[:400])
    return dict(r.json())


async def _failed_parquet(c: httpx.AsyncClient, org: str, ns: str) -> str:
    """A Parquet file uploaded, ingested and its job run here with no extractor configured:
    its source_uri. The job is run by itself, as a pass that took it runs it, since a pass
    takes one job of a queue other tests' jobs share."""
    from server.core.ch import get_client
    from server.knowledge.app import ingest_run
    from server.knowledge.store import ingestions
    from server.settings import settings

    data = b"plain words, not a parquet file"
    up = await c.post(
        "/v1/knowledge.upload",
        params={
            "org": org,
            "namespace": ns,
            "filename": "report.parquet",
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    assert up.status_code == 201, up.text
    uri = "repo://report.parquet"
    job = await _op(
        c,
        "knowledge.ingest",
        org=org,
        namespace=ns,
        upload_id=up.json()["upload_id"],
        source_uri=uri,
    )
    ch = get_client()
    await ch.connect()
    try:
        row = await ingestions.get_ingestion(org, ns, job["ingestion_id"])
        assert row is not None, job["ingestion_id"]
        with pytest.MonkeyPatch.context() as m:
            m.setattr(settings, "document_extractor_url", "")
            m.setattr(settings, "document_extractors_json", "{}")
            ran = await ingest_run.take_and_run(row)
        assert ran is not None, f"job {job['ingestion_id']} did not run"
    finally:
        await ch.disconnect()
    status = await _op(
        c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=job["ingestion_id"]
    )
    assert status["error_code"] == "format_unavailable", status
    return uri


@pytest.mark.asyncio
async def test_tune_cuts_the_documents_again_and_writes_nothing(
    control_plane_url: str, ch_client: Any
) -> None:
    org, ns = unique_org("tune-docs"), f"td-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=120) as c:
        for i in range(6):
            content = "\n\n".join(f"# Part {p}\n\n{SECTION}" for p in range(3))
            await _op(
                c,
                "knowledge.upsert",
                org=org,
                namespace=ns,
                source_uri=f"docs/{i}.md",
                content=content,
                title=f"Doc {i}",
            )
        pq = await _failed_parquet(c, org, ns)

        def snapshot() -> list[tuple[Any, ...]]:
            docs = ch_client.query(
                "SELECT source_uri, version, content_sha256, superseded_at FROM loom.documents"
                " FINAL WHERE org = {org:String} AND namespace = {ns:String} ORDER BY source_uri",
                parameters={"org": org, "ns": ns},
            ).result_rows
            chunks = ch_client.query(
                "SELECT count() FROM loom.chunks WHERE org = {org:String}"
                " AND namespace = {ns:String}",
                parameters={"org": org, "ns": ns},
            ).result_rows
            return [*docs, *chunks]

        formats = await _op(c, "knowledge.formats", org=org, namespace=ns)
        before = snapshot()
        out = await _op(c, "namespace.tune", org=org, namespace=ns)
        again = await _op(c, "namespace.tune", org=org, namespace=ns)
        assert snapshot() == before

    sample = out["chunking"]
    assert sample["documents"] == 6 and sample["unread"] == 0 and sample["population"] == 6
    assert sample["cap"] == 50
    cur = sample["current"]
    assert cur["sections"] == 18 and cur["sections_cut"] == 18
    assert cur["chunks"] > 18 and cur["max_chars"] <= cur["size"]
    # The test embedder states no input limit: nothing over it is counted, and no larger
    # size is drafted.
    assert sample["input_token_limit"] is None and cur["over_input_limit"] is None
    assert again["chunking"] == sample
    targets = {p["target"] for p in out["proposals"]}
    assert "settings.knowledge_chunking" not in targets
    skipped = out["skipped"]
    assert any(s.startswith("chunking:") and "input limit is not known" in s for s in skipped)
    # The failed source is said, and no route is invented: the server's extractor
    # declares parquet now (the reference extractor does), or none does.
    assert "settings.document_extractors_json" not in targets
    served = {f["format"]: f["available"] for f in formats["formats"]}["parquet"]
    said = (
        "the service they go to declares it now"
        if served
        else "no configured extractor declares parquet"
    )
    assert any(
        s.startswith("extractor.parquet: 1 source failed format_unavailable") and said in s
        for s in skipped
    ), skipped
    assert any(s.startswith("decision_extraction:") for s in skipped)
    # No document text comes back.
    assert "word word" not in str(out) and pq not in str(sample)
