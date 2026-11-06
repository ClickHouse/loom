"""The SDK's file ingest: `knowledge.upload` sends the bytes as they are with
their sha256, `knowledge.ingest` names the upload, and `wait_for_ingest`
follows the job until it ends."""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from loom import Agent, SyncAgent


class _Server:
    """A scripted server: what each route was sent, and jobs that end in
    `state` after `running` polls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, state: str = "ready", running: int = 1):
        self.state, self.running = state, running
        self.requests: list[httpx.Request] = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(self._handle)),
        )

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        op = req.url.path.removeprefix("/v1/")
        if op == "knowledge.upload":
            return httpx.Response(201, json={"upload_id": "up-1", "size": len(req.content)})
        if op == "knowledge.ingest":
            return httpx.Response(200, json={"ingestion_id": "job-1", "state": "pending"})
        if op == "knowledge.ingest_status":
            polls = sum(r.url.path.endswith("ingest_status") for r in self.requests)
            state = "processing" if polls <= self.running else self.state
            return httpx.Response(200, json={"found": True, "state": state, "revision": 2})
        return httpx.Response(200, json={"found": True, "cancelled": True})

    def body(self, op: str) -> dict[str, Any]:
        (req,) = [r for r in self.requests if r.url.path == f"/v1/{op}"]
        return dict(json.loads(req.content))


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    real_sleep = asyncio.sleep

    async def _sleep(delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("loom.knowledge.asyncio.sleep", _sleep)


def _agent() -> Agent:
    return Agent("acme", "support", "a", base_url="http://loom.test", api_key="k", auto_run=False)


async def test_upload_sends_the_bytes_as_they_are_with_their_sha256(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server(monkeypatch)
    data = b"%PDF-1.7 \x00\xff raw"
    async with _agent() as a:
        up = await a.knowledge.upload(data, "spec.pdf", content_type="application/pdf")
    assert up["upload_id"] == "up-1"
    (req,) = server.requests
    assert req.method == "POST" and req.content == data
    assert req.headers["Content-Type"] == "application/octet-stream"
    assert req.headers["Authorization"] == "Bearer k"
    assert dict(req.url.params) == {
        "org": "acme",
        "namespace": "support",
        "filename": "spec.pdf",
        "content_type": "application/pdf",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


async def test_ingest_names_the_upload_and_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _Server(monkeypatch)
    async with _agent() as a:
        job = await a.knowledge.ingest(
            "up-1",
            source_uri="repo://spec.pdf",
            tags=["rfc"],
            expected_revision=1,
            options={"format": "markdown"},
        )
    assert job["ingestion_id"] == "job-1"
    body = server.body("knowledge.ingest")
    assert (body["org"], body["namespace"], body["upload_id"]) == ("acme", "support", "up-1")
    assert (body["source_uri"], body["tags"], body["expected_revision"]) == (
        "repo://spec.pdf",
        ["rfc"],
        1,
    )
    assert body["options"] == {"format": "markdown"} and body["allow_partial"] is False


async def test_wait_for_ingest_returns_the_status_once_the_job_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server(monkeypatch, state="partial", running=3)
    async with _agent() as a:
        status = await a.knowledge.wait_for_ingest("job-1")
    assert (status["state"], status["revision"]) == ("partial", 2)
    assert len(server.requests) == 4  # three polls while it ran, one that saw it end


async def test_wait_for_ingest_times_out_while_the_job_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Server(monkeypatch, running=10**6)
    async with _agent() as a:
        with pytest.raises(TimeoutError, match="job-1 is still processing"):
            await a.knowledge.wait_for_ingest("job-1", timeout_s=0)


async def test_ingest_file_uploads_ingests_and_waits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = tmp_path / "runbook.md"
    doc.write_bytes(b"# Runbook")
    server = _Server(monkeypatch)
    async with _agent() as a:
        status = await a.knowledge.ingest_file(doc, title="Runbook")
        queued = await a.knowledge.ingest_file(doc, source_uri="repo://r.md", wait=False)
    assert status["state"] == "ready" and queued["state"] == "pending"
    first, second = [r for r in server.requests if r.url.path == "/v1/knowledge.ingest"]
    # The file's path names it, as the CLI's documents are named.
    assert json.loads(first.content)["source_uri"] == f"file://{doc.resolve()}"
    assert json.loads(first.content)["title"] == "Runbook"
    assert json.loads(second.content)["source_uri"] == "repo://r.md"
    uploads = [r for r in server.requests if r.url.path == "/v1/knowledge.upload"]
    assert [r.url.params["filename"] for r in uploads] == ["runbook.md", "runbook.md"]


def test_the_sync_agent_ingests_a_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = tmp_path / "n.txt"
    doc.write_text("hello")
    _Server(monkeypatch)
    with SyncAgent("acme", "support", "a", base_url="http://loom.test", api_key="k") as a:
        assert a.knowledge.ingest_file(doc)["state"] == "ready"
        assert a.knowledge.ingest_cancel("job-1")["cancelled"] is True
