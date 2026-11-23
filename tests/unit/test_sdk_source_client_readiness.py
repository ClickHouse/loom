"""Request admission and response capture share the real HTTPX transport hooks."""

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
    observation_scope,
)
from loom.transport import Transport

HEADER = "x-loom-session-protection"
PRIVATE = "private-source-value"


def client(root, respond):
    transport = Transport(
        "https://loom.invalid", "test-secret", pacing=False, session_protection=root
    )
    # Preserve the real hooks, auth, header merge and response processing.
    transport._client._transport = httpx.MockTransport(respond)
    transport._client._mounts.clear()
    return transport


@pytest.mark.asyncio
async def test_actual_source_request_declares_only_bound_ready_family(tmp_path: Path):
    root = SessionProtection.create(base=tmp_path / "families")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"mirrored": True, "content": PRIVATE})

    transport = client(root, respond)
    try:
        body = await transport.post("/v1/knowledge.source.mcp_read", {"namespace": "source"})
        assert body["content"] == PRIVATE
        assert requests[0].url.path == "/v1/knowledge.source.mcp_read"
        assert requests[0].headers.get_list(HEADER) == ["v1"]
        assert requests[0].headers["authorization"] == "Bearer test-secret"
        assert root.protected
        with pytest.raises(AutomaticCaptureSuppressed), root.automatic():
            await transport.post("/v1/memory.set", {"namespace": "elsewhere", "value": PRIVATE})
        assert len(requests) == 1
        # Explicit actions remain observed, including after token/session changes.
        transport._client.headers["Authorization"] = "Bearer changed-secret"
        await transport.post("/v1/memory.get", {"org": "other", "session_id": "other"})
        assert root.protected
        assert requests[-1].headers["authorization"] == "Bearer changed-secret"
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_ordinary_requests_make_no_promise():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = client(None, respond)
    try:
        assert await transport.post("/v1/memory.get", {}) == {"ok": True}
        assert HEADER not in requests[0].headers
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_open_callback_cannot_publish_with_a_new_unbound_client(tmp_path: Path):
    root = SessionProtection.create(base=tmp_path / "families")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers.get_list(HEADER) == ["v1"]
        return httpx.Response(200, json={"mirrored": True, "value": PRIVATE})

    reader = client(root, respond)
    writer = client(None, respond)
    try:
        with root.automatic():
            await reader.post("/v1/knowledge.source.mcp_read", {})
            with pytest.raises(AutomaticCaptureSuppressed):
                await writer.post("/v1/memory.set", {"namespace": "elsewhere", "value": PRIVATE})
        assert len(requests) == 1
        assert root.protected
    finally:
        await reader.close()
        await writer.close()


@pytest.mark.asyncio
async def test_unbound_source_read_preserves_server_readiness_refusal():
    def respond(request):
        assert request.url.path == "/v1/knowledge.source.mcp_read"
        assert HEADER not in request.headers
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "SESSION_PROTECTION_REQUIRED",
                    "message": "Session protection required",
                }
            },
        )

    transport = client(None, respond)
    try:
        with pytest.raises(LoomError) as caught:
            await transport.post("/v1/knowledge.source.mcp_read", {})
        assert caught.value.code == "SESSION_PROTECTION_REQUIRED"
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize(
    "headers", [{HEADER: "v1"}, {HEADER: "v2"}, {HEADER: "v1", HEADER.upper(): "v1"}]
)
async def test_caller_cannot_supply_or_duplicate_declaration(tmp_path: Path, bound, headers):
    root = SessionProtection.create(base=tmp_path / "families") if bound else None
    requests = []
    transport = client(root, lambda request: requests.append(request))
    try:
        with pytest.raises(SessionProtectionUnavailable):
            await transport.post_with_meta("/v1/memory.get", {}, extra_headers=headers)
        assert requests == []
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_callback_cannot_manufacture_readiness_and_failure_restores_scope(tmp_path: Path):
    root = SessionProtection.create(base=tmp_path / "families")
    transport = client(root, lambda request: httpx.Response(200, json={"ok": True}))

    async def callback(path, payload):
        raise RuntimeError("callback failed")

    try:
        transport.before_post = callback
        with root.automatic(), pytest.raises(RuntimeError, match="callback failed"):
            await transport.post("/v1/memory.get", {})
        assert not root.protected
        transport.before_post = None
        plain = client(
            None, lambda request: httpx.Response(200, json={"declared": HEADER in request.headers})
        )
        try:
            assert await plain.post("/v1/memory.get", {}) == {"declared": False}
        finally:
            await plain.close()
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "incompatible", "observer"])
async def test_invalid_control_or_missing_observer_refuses_before_network(tmp_path: Path, failure):
    root = SessionProtection.create(base=tmp_path / "families")
    requests = []
    transport = client(root, lambda request: requests.append(request))
    if failure == "missing":
        await asyncio.to_thread((root.base / root.id / "state").unlink)
    elif failure == "incompatible":
        await asyncio.to_thread((root.base / root.id / "state").write_bytes, b"unknown")
    else:
        transport._client.event_hooks["response"].clear()
    try:
        with pytest.raises(SessionProtectionUnavailable):
            await transport.post("/v1/knowledge.source.mcp_read", {})
        assert requests == []
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_cross_origin_refusal_and_independent_families(tmp_path: Path):
    first = SessionProtection.create(base=tmp_path / "families")
    other = SessionProtection.create(base=tmp_path / "families")
    requests = []
    transport = client(first, lambda request: requests.append(request))
    try:
        with pytest.raises(SessionProtectionUnavailable):
            await transport._client.get("https://upstream.invalid/resource")
        assert requests == []
        first.mark()
        second = client(other, lambda request: httpx.Response(200, json={"ok": True}))
        try:
            with other.automatic():
                assert await second.post("/v1/memory.get", {}) == {"ok": True}
            assert not other.protected
            with observation_scope((first,)):
                assert await second.post("/v1/memory.get", {}) == {"ok": True}
            assert first.protected and not other.protected
        finally:
            await second.close()
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403])
async def test_json_errors_observed_without_private_diagnostics(tmp_path: Path, status):
    root = SessionProtection.create(base=tmp_path / "families")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, content=PRIVATE, headers={"content-type": "application/json"})

    transport = client(root, respond)
    try:
        with pytest.raises(SessionProtectionUnavailable) as caught:
            await transport.post("/v1/knowledge.source.mcp_read", {})
        assert PRIVATE not in str(caught.value)
        assert root.protected
        assert requests[0].headers[HEADER] == "v1"
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_error_data_marks_before_typed_error_and_preserves_headers(tmp_path: Path):
    root = SessionProtection.create(base=tmp_path / "families")
    transport = client(
        root,
        lambda request: httpx.Response(
            403,
            json={
                "error": {"code": "FORBIDDEN", "message": "refused", "data": {"mirrored": True}},
            },
            headers={"X-Request-Id": "protected-error"},
        ),
    )
    try:
        with pytest.raises(LoomError) as caught:
            await transport.post("/v1/knowledge.source.mcp_read", {})
        assert root.protected
        assert caught.value.request_id == "protected-error"
    finally:
        await transport.close()


class Events(httpx.AsyncByteStream):
    def __init__(self, mode):
        self.mode = mode
        self.closed = False

    async def __aiter__(self):
        if self.mode == "cancel":
            raise asyncio.CancelledError()
        yield b'data: {"mirrored":true,"content":"private-source-value"}\n\n'
        if self.mode == "partial":
            yield b'data: {"content":"unfinished'

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["complete", "partial", "cancel", "early"])
async def test_stream_admission_observation_and_owned_cleanup(tmp_path: Path, mode):
    root = SessionProtection.create(base=tmp_path / "families")
    events = Events(mode)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            stream=events,
            headers={"content-type": "text/event-stream", "X-Request-Id": "events"},
        )

    transport = client(root, respond)
    try:

        async def consume():
            async with transport.stream("/v1/source-events", json={}) as response:
                assert response.headers["X-Request-Id"] == "events"
                if mode == "early":
                    return
                async for chunk in response.aiter_bytes():
                    assert root.protected
                    assert PRIVATE.encode() in chunk

        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await consume()
        elif mode == "partial":
            with pytest.raises(SessionProtectionUnavailable):
                await consume()
        else:
            await consume()
        assert requests[0].headers.get_list(HEADER) == ["v1"]
        assert root.protected
        assert events.closed
        with pytest.raises(AutomaticCaptureSuppressed), root.automatic():
            await transport.post("/v1/memory.set", {"namespace": "elsewhere", "value": PRIVATE})
    finally:
        await transport.close()
