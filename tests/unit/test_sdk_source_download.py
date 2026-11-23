"""Opaque GET bytes retain durable response authority through the actual SDK hooks."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from loom.errors import LoomError
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
)

from tests.unit.test_sdk_source_client_readiness import HEADER, PRIVATE, client


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, hold: bool = False):
        self.chunks = chunks
        self.hold = hold
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.hold:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media",
    [
        "text/plain",
        "application/pdf",
        "application/octet-stream",
        "application/json",
        "application/x-ndjson",
    ],
)
async def test_opaque_download_marks_before_release_and_reopens(tmp_path: Path, media: str):
    root = SessionProtection.create(base=tmp_path / "families")
    sent = []
    stream = Body([PRIVATE.encode(), b"\nnot an envelope\n"])

    def respond(request):
        sent.append(request)
        assert request.method == "GET" and request.url.path == "/v1/knowledge.download"
        assert request.url.params["namespace"] == "source"
        assert request.headers.get_list(HEADER) == ["v1"]
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200, headers={"Content-Type": media, "Content-Disposition": "attachment"}, stream=stream
        )

    transport = client(root, respond)
    writer = client(None, respond)
    try:
        response = await transport.download(
            "/v1/knowledge.download", params={"namespace": "source"}
        )
        assert root.protected and SessionProtection.open(root.pointer()).protected
        assert response.content == PRIVATE.encode() + b"\nnot an envelope\n"
        assert response.headers["content-disposition"] == "attachment"
        assert stream.closed and len(sent) == 1
        with pytest.raises(AutomaticCaptureSuppressed), root.automatic():
            await writer.post("/v1/memory.set", {"namespace": "other", "value": PRIVATE})
        assert len(sent) == 1
    finally:
        await transport.close()
        await writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bytes", "chunks", "encoding", "error"])
async def test_download_failure_retains_protection_and_closes(tmp_path: Path, mode: str):
    root = SessionProtection.create(base=tmp_path / "families")
    stream = Body([b"x"] * 4097 if mode == "chunks" else [b"123456789"])
    headers = {"Content-Type": "application/octet-stream"}
    if mode == "encoding":
        headers["Content-Encoding"] = "gzip"
    if mode == "error":
        stream = Body(
            [b'{"error":{"code":"FAILED","message":"private diagnostic"},"mirrored":true}']
        )
        headers = {"Content-Type": "application/json"}
    transport = client(
        root,
        lambda request: httpx.Response(
            403 if mode == "error" else 200, headers=headers, stream=stream
        ),
    )
    try:
        with pytest.raises((SessionProtectionUnavailable, LoomError)):
            await transport.download(
                "/v1/knowledge.download", max_bytes=8 if mode == "bytes" else 8192
            )
        assert root.protected and stream.closed
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_cancelled_download_joins_owned_body(tmp_path: Path):
    root = SessionProtection.create(base=tmp_path / "families")
    stream = Body([b"prefix"], hold=True)
    transport = client(
        root,
        lambda request: httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=stream),
    )
    task = asyncio.create_task(transport.download("/v1/knowledge.download"))
    try:
        await asyncio.wait_for(stream.waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert root.protected and stream.closed
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await transport.close()


@pytest.mark.asyncio
async def test_unbound_public_download_has_no_declaration():
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, content=b"ordinary bytes")

    transport = client(None, respond)
    try:
        assert (await transport.download("/download")).content == b"ordinary bytes"
        assert HEADER not in sent[0].headers
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_download_does_not_release_bytes_when_marking_fails(tmp_path: Path, monkeypatch):
    root = SessionProtection.create(base=tmp_path / "families")
    stream = Body([PRIVATE.encode()])
    transport = client(root, lambda request: httpx.Response(200, stream=stream))

    def fail(self):
        raise SessionProtectionUnavailable()

    monkeypatch.setattr(SessionProtection, "mark", fail)
    try:
        with pytest.raises(SessionProtectionUnavailable):
            await transport.download("/download")
        assert stream.closed
    finally:
        await transport.close()
