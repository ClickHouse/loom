"""Faults on the next revision of a source, end to end on the server's
ClickHouse: revision 1 of a SQL file is published, a second ingest meets one
fault, and the job ends in its typed state while knowledge.get and
knowledge.search still serve revision 1 and none of revision 2.

The job runs in this process, as a knowledge_ingest worker pass runs one, with
the reference extractor served in process too, so the faults are injected where
the job meets them: the extractor's answer, the object store's read, the
embedder, and the document row's insert after the chunks are written. The
unit matrix (tests/unit/test_knowledge_ingest.py) covers every fault; these
cases prove the stores' half (chunk retirement, FINAL reads) on a real server.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

FIRST = b"-- Raw events are kept for 30 days.\nCREATE TABLE marmot_events (id UInt64) ENGINE = MergeTree ORDER BY id;\n"
SECOND = b"-- Raw events are kept for 90 days.\nCREATE TABLE quokka_events (id UInt64) ENGINE = MergeTree ORDER BY id;\n"


@pytest.fixture
def reference_extractor(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The reference extractor in this process, as the job's only service."""
    from loom_extractor import service
    from server.knowledge.app import extractors
    from server.settings import settings

    token = "t" * 24
    app = service.create_app(service.Config(token=token))
    monkeypatch.setattr(extractors, "transport", httpx.ASGITransport(app=app))
    monkeypatch.setattr(settings, "document_extractor_url", "http://extractor:8790")
    monkeypatch.setattr(settings, "document_extractors_json", "{}")
    monkeypatch.setattr(settings, "document_extractor_token", token)
    monkeypatch.setattr(settings, "document_extractor_tokens_json", "{}")
    extractors._declared.clear()
    yield
    extractors._declared.clear()


async def _op(c: httpx.AsyncClient, op: str, **body: Any) -> dict[str, Any]:
    r = await c.post(f"/v1/{op}", json=body)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _ingest(c: httpx.AsyncClient, org: str, ns: str, uri: str, data: bytes) -> str:
    r = await c.post(
        "/v1/knowledge.upload",
        params={
            "org": org,
            "namespace": ns,
            "filename": "retention.sql",
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    assert r.status_code == 201, r.text
    job = await _op(
        c,
        "knowledge.ingest",
        org=org,
        namespace=ns,
        upload_id=r.json()["upload_id"],
        source_uri=uri,
    )
    return str(job["ingestion_id"])


async def _work(org: str, ns: str, ingestion_id: str) -> str | None:
    from server.core.ch import get_client
    from server.knowledge.app import ingest_run
    from server.knowledge.store import ingestions

    ch = get_client()
    await ch.connect()
    try:
        job = await ingestions.get_ingestion(org, ns, ingestion_id)
        assert job is not None, ingestion_id
        return await ingest_run.take_and_run(job)
    finally:
        await ch.disconnect()


def _inject(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import BackendUnavailableError
    from server.core.object_store import ObjectStoreError
    from server.knowledge.app import extractors
    from server.knowledge.store import blobs
    from server.knowledge.store import documents as documents_repo

    if fault == "extractor down":

        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable", request=request)

        monkeypatch.setattr(extractors, "transport", httpx.MockTransport(unreachable))
    elif fault == "artifact out of contract":
        reference = extractors.transport
        assert reference is not None

        class OutOfContract(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                response = await reference.handle_async_request(request)
                if not request.url.path.endswith("/v1/extract"):
                    return response
                await response.aread()
                artifact = response.json()
                artifact["blocks"][-1]["parent"] = "no-such-block"
                # Unread, as the network gives it: the client reads it as a stream.
                body = httpx.ByteStream(json.dumps(artifact).encode())
                return httpx.Response(200, stream=body)

        monkeypatch.setattr(extractors, "transport", OutOfContract())
    elif fault == "bucket fails the original's read":

        async def bucket_fails(*a: Any, **k: Any) -> bytes:
            raise ObjectStoreError("get failed: ServiceUnavailable", operation="read")

        monkeypatch.setattr(blobs, "read", bucket_fails)
    elif fault == "embedder down":
        from server.embedding import get_embedder

        async def down(texts: list[str]) -> list[list[float]]:
            raise BackendUnavailableError("provider down", operation="embed")

        monkeypatch.setattr(get_embedder(), "embed_many", down)
    elif fault == "document row refused after the chunks":

        async def refused(*a: Any, **k: Any) -> None:
            raise ValueError("the document row is refused")

        monkeypatch.setattr(documents_repo, "insert_one", refused)
    else:
        raise AssertionError(fault)


FAULTS = [
    ("extractor down", "retries_exhausted"),
    ("artifact out of contract", "invalid_artifact"),
    ("bucket fails the original's read", "retries_exhausted"),
    ("embedder down", "retries_exhausted"),
    ("document row refused after the chunks", "internal"),
]


@pytest.mark.usefixtures("reference_extractor")
@pytest.mark.parametrize(("fault", "code"), FAULTS, ids=[f for f, _ in FAULTS])
@pytest.mark.asyncio
async def test_a_fault_on_the_next_revision_leaves_the_earlier_one_serving(
    control_plane_url: str, monkeypatch: pytest.MonkeyPatch, fault: str, code: str
) -> None:
    from server.settings import settings

    org, ns = unique_org("ingest-fault"), "docs"
    uri = f"repo://retention-{uuid.uuid4().hex[:6]}.sql"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        assert await _work(org, ns, await _ingest(c, org, ns, uri, FIRST)) == "ready"

        second = await _ingest(c, org, ns, uri, SECOND)
        with monkeypatch.context() as m:
            m.setattr(settings, "knowledge_ingest_max_attempts", 1)
            _inject(fault, m)
            assert await _work(org, ns, second) == "failed"

        status = await _op(c, "knowledge.ingest_status", org=org, namespace=ns, ingestion_id=second)
        assert (status["state"], status["error_code"]) == ("failed", code), status
        assert status["error_message"]

        doc = await _op(c, "knowledge.get", org=org, namespace=ns, source_uri=uri)
        assert doc["revision"] == 1 and "marmot_events" in doc["content"]
        assert "quokka_events" not in doc["content"]

        for term, served in (("marmot_events", True), ("quokka_events", False)):
            hits = await _op(
                c,
                "knowledge.search",
                org=org,
                namespace=ns,
                query=term,
                namespace_scope="this",
                search_mode="lexical",
            )
            found = [h for h in hits["results"] if term in h["content"]]
            assert bool(found) is served, (term, hits["results"])
