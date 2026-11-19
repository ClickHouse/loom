"""Stream frames protect retained producers before any caller receives their bytes."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from loom.session_protection import SessionProtection, SessionProtectionUnavailable
from loom.transport import Transport


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


def client(root, chunks):
    transport = Transport("http://inert.invalid", session_protection=root)
    body = Chunks(chunks)
    transport._client._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, stream=body, headers={"content-type": "text/event-stream"}
        )
    )
    return transport, body


@contextlib.asynccontextmanager
async def closing(transport):
    try:
        yield transport
    finally:
        await transport.close()


async def test_marked_frame_is_durable_before_first_raw_bytes(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    frame = b'data: {"mirrored":true,"text":"private"}\n\n'
    transport, body = client(root, [frame[:17], frame[17:], b"data: [DONE]\n\n"])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            chunks = response.aiter_bytes()
            assert await anext(chunks) == frame
            assert root.protected
            assert await anext(chunks) == b"data: [DONE]\n\n"
            with pytest.raises(StopAsyncIteration):
                await anext(chunks)
    assert body.closed


async def test_stream_retains_all_opening_producers_after_rebind_and_context_exit(tmp_path):
    first = SessionProtection.create(base=tmp_path / "first")
    second = SessionProtection.create(base=tmp_path / "second")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    transport, body = client(second, [b'data: {"mirrored":true}\n\n', b"data: [DONE]\n\n"])
    async with closing(transport):
        stream = transport.stream("/v1/assistant/chat/completions", json={})
        with first.automatic():
            response = await stream.__aenter__()
        transport.session_protection = replacement
        try:
            assert await anext(response.aiter_bytes()) == b'data: {"mirrored":true}\n\n'
            assert first.protected and second.protected
            assert not replacement.protected
        finally:
            await stream.__aexit__(None, None, None)
    assert body.closed


@pytest.mark.parametrize(
    "frame", [b'data: {"mirrored":true\n\n', b'data: {"mirrored":"true"}\n\n', b"data: []\n\n"]
)
async def test_unverifiable_frame_protects_before_withholding(tmp_path, frame):
    root = SessionProtection.create(base=tmp_path / "root")
    transport, body = client(root, [frame])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            with pytest.raises(
                SessionProtectionUnavailable,
                match="^Session protection state could not be confirmed$",
            ):
                await anext(response.aiter_bytes())
    assert root.protected and body.closed


async def test_partial_frame_at_eof_is_not_released(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    transport, body = client(root, [b'data: {"mirrored":true,"text":"private"}'])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            with pytest.raises(SessionProtectionUnavailable):
                await anext(response.aiter_bytes())
    assert root.protected and body.closed


async def test_crlf_multiline_unicode_frames_remain_incremental_and_byte_exact(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    frame = 'data: {"mirrored":false,\r\ndata: "text":"日本語"}\r\n\r\n'.encode()
    transport, body = client(root, [bytes([byte]) for byte in frame] + [b"data: [DONE]\n\n"])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            assert (
                b"".join([chunk async for chunk in response.aiter_bytes()])
                == frame + b"data: [DONE]\n\n"
            )
    assert not root.protected and body.closed


async def test_failed_first_mark_still_protects_another_opening_family(tmp_path):
    first = SessionProtection.create(base=tmp_path / "first")
    second = SessionProtection.create(base=tmp_path / "second")
    transport, body = client(second, [b'data: {"mirrored":true}\n\n'])
    async with closing(transport):
        with first.automatic():
            async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
                (first.base / first.id / "state").unlink()
                with pytest.raises(SessionProtectionUnavailable):
                    await anext(response.aiter_bytes())
    assert second.protected and body.closed


async def test_unbound_raw_stream_keeps_ordinary_bytes(tmp_path):
    transport, body = client(None, [b"not a canonical SSE frame"])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            assert await response.aread() == b"not a canonical SSE frame"
    assert body.closed


async def test_generic_buffered_request_cannot_bypass_sse_observation(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    frame = b'data: {"mirrored":true,"text":"private"}\n\ndata: [DONE]\n\n'
    transport, body = client(root, [frame])
    async with closing(transport):
        response = await transport.request(
            "POST", "/v1/assistant/chat/completions", json={"stream": True}
        )
        assert root.protected
        assert response.content == frame
        assert response.request.headers["Accept-Encoding"] == "identity"
    assert body.closed


async def test_cached_custom_response_is_observed_before_response_exposure(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    transport, _ = client(root, [])
    frame = b'data: {"mirrored":true}\n\ndata: [DONE]\n\n'
    transport._client._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=frame, headers={"content-type": "text/event-stream"}
        )
    )
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            assert root.protected
            assert await response.aread() == frame


async def test_encoded_bound_stream_refuses_before_exposing_raw_bytes(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    transport, body = client(root, [b"unread encoded bytes"])
    transport._client._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            stream=body,
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
        )
    )
    async with closing(transport):
        with pytest.raises(SessionProtectionUnavailable):
            async with transport.stream("/v1/assistant/chat/completions", json={}):
                pytest.fail("an encoded stream escaped observation")
    assert root.protected and body.closed


async def test_bom_comments_and_cr_only_frames_preserve_wire_bytes(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    frame = b'\xef\xbb\xbf: keepalive\r\rdata: {"mirrored":true}\r\r'
    transport, body = client(root, [bytes([byte]) for byte in frame] + [b"data: [DONE]\r\r"])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            received = b"".join([chunk async for chunk in response.aiter_bytes()])
            assert received == frame + b"data: [DONE]\r\r"
            assert root.protected
    assert body.closed


async def test_invalid_utf8_protects_without_releasing_frame(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    transport, body = client(root, [b'data: {"text":"\xff"}\n\n'])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            with pytest.raises(SessionProtectionUnavailable):
                await anext(response.aiter_bytes())
    assert root.protected and body.closed


async def test_oversized_unfinished_frame_protects_at_existing_envelope_bound(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    transport, body = client(root, [b"data: " + b"x" * (4 * 1024 * 1024)])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            with pytest.raises(SessionProtectionUnavailable):
                await anext(response.aiter_bytes())
    assert root.protected and body.closed


async def test_interrupted_unverifiable_frame_protects_before_typed_network_error(tmp_path):
    from loom.errors import LoomError

    root = SessionProtection.create(base=tmp_path / "root")
    transport, body = client(root, [b'data: {"mirrored":tr', httpx.ReadError("connection cut")])
    async with closing(transport):
        with pytest.raises(LoomError):
            async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
                await anext(response.aiter_bytes())
    assert root.protected and body.closed


@pytest.mark.parametrize("error_kind", ["stream", "json"])
async def test_assistant_typed_error_observes_private_diagnostic_first(tmp_path, error_kind):
    import json
    from types import SimpleNamespace

    from loom.assistant import AssistantClient
    from loom.errors import BackendUnavailable

    root = SessionProtection.create(base=tmp_path / "root")
    envelope = {"mirrored": True, "error": {"message": "private diagnostic", "status": 503}}
    transport, body = client(root, [("data: " + json.dumps(envelope) + "\n\n").encode()])
    if error_kind == "json":
        transport._client._transport = httpx.MockTransport(
            lambda request: httpx.Response(
                503, json={"mirrored": True, "detail": "private diagnostic"}
            )
        )
    assistant = AssistantClient(SimpleNamespace(transport=transport, namespace="notes"))
    async with closing(transport):
        with pytest.raises(BackendUnavailable, match="private diagnostic"):
            await anext(assistant.stream("question"))
        assert root.protected
    assert error_kind == "json" or body.closed


async def test_actual_assistant_text_marks_before_yield_and_blocks_next_automatic_dispatch(
    tmp_path,
):
    from types import SimpleNamespace

    from loom.assistant import AssistantClient
    from loom.session_protection import AutomaticCaptureSuppressed

    root = SessionProtection.create(base=tmp_path / "root")
    frame = b'data: {"mirrored":true}\n\ndata: {"id":"answer","choices":[{"delta":{"content":"private"}}]}\n\n'
    transport, body = client(root, [frame, b"data: [DONE]\n\n"])
    assistant = AssistantClient(SimpleNamespace(transport=transport, namespace="notes"))
    async with closing(transport):
        with root.automatic():
            events = assistant.stream("question")
            try:
                assert (await anext(events)).text == "private"
                assert root.protected
                with pytest.raises(AutomaticCaptureSuppressed):
                    await transport.request("POST", "/v1/conversation.append", json={})
                assert len([event async for event in events]) == 1
            finally:
                await events.aclose()
    assert body.closed


@pytest.mark.parametrize("failure", ["read", "cancel"])
async def test_started_stream_keeps_primary_failure_and_closes_response(tmp_path, failure):
    from loom.errors import LoomError

    root = SessionProtection.create(base=tmp_path / "root")
    error = httpx.ReadError("later read failed") if failure == "read" else asyncio.CancelledError()
    transport, body = client(root, [b'data: {"mirrored":true}\n\n', error])
    async with closing(transport):
        with pytest.raises(LoomError if failure == "read" else asyncio.CancelledError):
            async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
                chunks = response.aiter_bytes()
                await anext(chunks)
                assert root.protected
                await anext(chunks)
    assert root.protected and body.closed


async def test_unicode_line_separators_inside_json_are_not_sse_boundaries(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    frame = 'data: {"mirrored":false,"text":"first\u2028second\u0085third\u2029last"}\n\n'.encode()
    transport, body = client(root, [frame, b"data: [DONE]\n\n"])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            assert await response.aread() == frame + b"data: [DONE]\n\n"
    assert not root.protected and body.closed


async def test_explicit_close_before_observation_protects_and_closes_owned_response(tmp_path):
    root = SessionProtection.create(base=tmp_path / "root")
    transport, body = client(root, [b'data: {"mirrored":true}\n\n'])
    async with closing(transport):
        async with transport.stream("/v1/assistant/chat/completions", json={}) as response:
            assert not root.protected
            await response.aclose()
            assert root.protected and body.closed
