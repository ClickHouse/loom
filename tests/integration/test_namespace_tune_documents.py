"""namespace.tune's document drafts against a live Loom + ClickHouse.

Six text documents whose sections are longer than the chunk size, and a PDF that no
configured extractor takes (ingested through the wire and run by a knowledge_ingest
worker pass in this process, so it fails `format_unavailable` as production fails it).
Tune cuts the documents again in process and reports the cut: every section is cut, and
no larger size is drafted because the test embedder states no input limit, which
`skipped` says. The PDF is named in `skipped` with no route invented, decision extraction
is said to take no instructions, the same sample comes back on a second call, and nothing
is written: the document rows and chunks are as they were.
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


async def _failed_pdf(c: httpx.AsyncClient, org: str, ns: str) -> str:
    """A PDF uploaded, ingested and run by a worker pass here: its source_uri."""
    from server.core.ch import get_client
    from server.knowledge.workers import ingest

    data = b"plain words, not a PDF"
    up = await c.post(
        "/v1/knowledge.upload",
        params={
            "org": org,
            "namespace": ns,
            "filename": "report.pdf",
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    assert up.status_code == 201, up.text
    uri = "repo://report.pdf"
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
        for _ in range(10):
            await ingest.run_pass()
            status = await _op(
                c,
                "knowledge.ingest_status",
                org=org,
                namespace=ns,
                ingestion_id=job["ingestion_id"],
            )
            if status["state"] != "pending" or status["attempts"]:
                break
    finally:
        await ch.disconnect()
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
        pdf = await _failed_pdf(c, org, ns)

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
    # No configured extractor declares pdf: said, and no route is invented.
    assert "settings.document_extractors_json" not in targets
    assert any(
        s.startswith("extractor.pdf: 1 source failed format_unavailable")
        and "no configured extractor declares pdf" in s
        for s in skipped
    ), skipped
    assert any(s.startswith("decision_extraction:") for s in skipped)
    # No document text comes back.
    assert "word word" not in str(out) and pdf not in str(sample)
