"""Cancellation must drain owned HTTP work before the client context closes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest

from server.clients import mcp_source as source
from tests.unit.clock_fake import ManualClock

ENDPOINT = "https://source.example/mcp"
IDENTITY = "doc://workspace/one"
TEXT = "selected body"
STRING = {"type": "string", "maxLength": 2048}
INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"id": STRING},
    "required": ["id"],
}
OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {name: STRING for name in ("id", "uri", "title", "text")},
    "required": ["id", "uri", "title", "text"],
}


async def access() -> None:
    return None


async def pin(*args: Any, **kwargs: Any) -> tuple[str, str, str, str]:
    return "", "https://93.184.216.34/mcp", "source.example", "source.example"


@dataclass
class HTTPFixture:
    kind: str
    block: bool = True
    phase: str = "headers"
    hold_cleanup: bool = False
    block_initialize: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    reads: int = 0
    requests: int = 0
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_release: asyncio.Event = field(default_factory=asyncio.Event)
    pool_closed: asyncio.Event = field(default_factory=asyncio.Event)
    pool_closes: int = 0

    async def cleanup(self) -> None:
        self.closing.set()
        if self.hold_cleanup:
            await self.cleanup_release.wait()
        self.stopped.set()

    def config(self) -> source.MCPSourceConfig:
        mapping = source.MCPToolMapping("read_document", INPUT, OUTPUT, "id")
        return source.MCPSourceConfig(
            ENDPOINT,
            approved_resource_ids=(IDENTITY,),
            approved_tools=("read_document",) if self.kind == "tools" else (),
            read=mapping if self.kind == "tools" else None,
            limits=source.MCPLimits(timeout_seconds=5, request_seconds=2),
        )

    async def reply(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if request.method == "GET":
            return httpx.Response(405)
        payload = json.loads(await request.aread())
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            if self.block_initialize:
                self.started.set()
                try:
                    await self.release.wait()
                finally:
                    await self.cleanup()
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {self.kind: {}},
                "serverInfo": {"name": "synthetic", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{"name": "read_document", "inputSchema": INPUT, "outputSchema": OUTPUT}]
            }
        elif method in {"resources/read", "tools/call"}:
            self.reads += 1
            if self.phase != "headers":
                return httpx.Response(
                    200,
                    stream=ReadStream(self, payload["id"]),
                    headers={
                        "content-type": "text/event-stream"
                        if self.phase == "sse"
                        else "application/json"
                    },
                )
            self.started.set()
            try:
                if self.block:
                    await self.release.wait()
                result = self.document()
            finally:
                await self.cleanup()
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def document(self) -> dict[str, Any]:
        if self.kind == "resources":
            return {"contents": [{"uri": IDENTITY, "text": TEXT}]}
        return {
            "content": [],
            "structuredContent": {"id": IDENTITY, "uri": IDENTITY, "title": "One", "text": TEXT},
        }

    def client(self) -> source.MCPSourceClient:
        return source.MCPSourceClient(
            self.config(),
            None,
            validate_access=access,
            _transport=FixtureTransport(self),
        )


class ReadStream(httpx.AsyncByteStream):
    def __init__(self, fixture: HTTPFixture, request_id: int) -> None:
        self.fixture, self.request_id = fixture, request_id

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.fixture.started.set()
        if self.fixture.block:
            await self.fixture.release.wait()
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self.request_id, "result": self.fixture.document()}
        ).encode()
        yield b"event: message\ndata: " + body + b"\n\n" if self.fixture.phase == "sse" else body

    async def aclose(self) -> None:
        await self.fixture.cleanup()


class FixtureTransport(httpx.MockTransport):
    def __init__(self, fixture: HTTPFixture) -> None:
        super().__init__(fixture.reply)
        self.fixture = fixture

    async def aclose(self) -> None:
        await super().aclose()
        self.fixture.pool_closes += 1
        self.fixture.pool_closed.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("phase", ["headers", "json", "sse"])
async def test_cancelled_read_drains_http_before_context_close(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    phase: str,
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture(kind, phase=phase)
    async with fixture.client() as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Capture the invariant before either releasing upstream or closing the client.
            stopped_inside_open_context = fixture.stopped.is_set()
            assert task.cancelled()
            assert fixture.pool_closed.is_set()
            before = fixture.reads
            with pytest.raises(source.MCPSourceError, match="closed"):
                await remote.read(IDENTITY)
            assert fixture.reads == before
        finally:
            fixture.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert stopped_inside_open_context, "cancelled read left its owned HTTP request running"
    assert fixture.pool_closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("phase", ["headers", "json", "sse"])
async def test_adjacent_normal_selected_read_returns_receipt(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    phase: str,
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture(kind, block=False, phase=phase)
    async with fixture.client() as remote:
        evidence = await remote.read(IDENTITY)
        assert evidence.text == TEXT
        assert evidence.receipt is not None
        assert evidence.receipt.requested_id == IDENTITY
        assert fixture.stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_repeated_cancellation_still_awaits_stream_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture(kind, phase="sse", hold_cleanup=True)
    async with fixture.client() as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.started.wait(), timeout=1)
            task.cancel()
            await asyncio.wait_for(fixture.closing.wait(), timeout=1)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not fixture.stopped.is_set()
            fixture.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert fixture.stopped.is_set()
            assert fixture.pool_closed.is_set()
        finally:
            fixture.release.set()
            fixture.cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_terminal_cancellation_does_not_stop_another_client(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    cancelled, neighbor = HTTPFixture(kind), HTTPFixture(kind)
    async with cancelled.client() as first, neighbor.client() as second:
        first_task = asyncio.create_task(first.read(IDENTITY))
        second_task = asyncio.create_task(second.read(IDENTITY))
        try:
            await asyncio.wait_for(cancelled.started.wait(), timeout=1)
            await asyncio.wait_for(neighbor.started.wait(), timeout=1)
            first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_task
            assert cancelled.stopped.is_set()
            assert not second_task.done()
            assert not neighbor.stopped.is_set()
            assert not neighbor.pool_closed.is_set()
            neighbor.release.set()
            evidence = await second_task
            assert evidence.text == TEXT
            assert evidence.receipt is not None
        finally:
            cancelled.release.set()
            neighbor.release.set()
            for task in (first_task, second_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first_task, second_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_cancellation_during_initialize_drains_lifecycle_owner(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture(kind, block_initialize=True)

    async def opening() -> None:
        async with fixture.client():
            pytest.fail("a blocked initialize unexpectedly completed")

    task = asyncio.create_task(opening())
    try:
        await asyncio.wait_for(fixture.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fixture.stopped.is_set()
        assert fixture.pool_closed.is_set()
        assert fixture.reads == 0
    finally:
        fixture.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_idle_session_deadline_closes_pool_before_context_exit(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    monkeypatch.setattr(loop, "time", clock.monotonic)
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture(kind, block=False)
    closed_inside = False
    exit_failure = None
    try:
        async with fixture.client() as remote:
            clock.advance(6)
            # All fixture I/O is ready; let the expired timer and SDK cleanup run.
            for _ in range(100):
                await asyncio.sleep(0)
                if fixture.pool_closed.is_set():
                    break
            closed_inside = fixture.pool_closed.is_set()
            if closed_inside:
                with pytest.raises(source.MCPSourceError) as refusal:
                    await remote.read(IDENTITY)
                assert refusal.value.code == "timeout"
                assert fixture.reads == 0
    except source.MCPSourceError as error:
        exit_failure = error.code
    assert closed_inside, "the session deadline left its idle HTTP pool open"
    assert exit_failure is None, "cleanup repeated an already reported deadline refusal"
    assert fixture.pool_closes == 1


class HeartbeatStream(ReadStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        fixture = self.fixture
        assert isinstance(fixture, HeartbeatFixture)
        fixture.started.set()
        while not fixture.release.is_set():
            fixture.beats += 1
            yield b": keepalive\n\n" if fixture.phase == "sse" else b" "
            await asyncio.sleep(0.01)


@dataclass
class HeartbeatFixture(HTTPFixture):
    beats: int = 0

    async def reply(self, request: httpx.Request) -> httpx.Response:
        response = await super().reply(request)
        if request.method == "POST":
            payload = json.loads(await request.aread())
            if payload["method"] in {"resources/read", "tools/call"}:
                response.stream = HeartbeatStream(self, payload["id"])
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("phase", ["json", "sse"])
async def test_active_request_deadline_joins_http_before_return(
    monkeypatch: pytest.MonkeyPatch, kind: str, phase: str
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HeartbeatFixture(kind, phase=phase)
    config = fixture.config()
    config = replace(config, limits=replace(config.limits, request_seconds=0.15))
    async with source.MCPSourceClient(
        config, None, validate_access=access, _transport=FixtureTransport(fixture)
    ) as remote:
        try:
            with pytest.raises(source.MCPSourceError) as refusal:
                await remote.read(IDENTITY)
            assert refusal.value.code == "timeout"
            assert fixture.beats >= 2
            assert fixture.stopped.is_set(), "request deadline left its HTTP stream active"
            assert fixture.pool_closed.is_set()
            beats, reads = fixture.beats, fixture.reads
            await asyncio.sleep(0.03)
            assert fixture.beats == beats
            with pytest.raises(source.MCPSourceError, match="timeout"):
                await remote.read(IDENTITY)
            assert fixture.reads == reads
        finally:
            fixture.release.set()
    assert fixture.pool_closes == 1


class RefusalStream(httpx.AsyncByteStream):
    def __init__(self, closed: asyncio.Event) -> None:
        self.closed = closed

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b""

    async def aclose(self) -> None:
        self.closed.set()


@dataclass
class StatefulFixture(HTTPFixture):
    refusal_closed: asyncio.Event = field(default_factory=asyncio.Event)

    async def reply(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            await self.started.wait()
            return httpx.Response(403, stream=RefusalStream(self.refusal_closed))
        response = await super().reply(request)
        if json.loads(await request.aread())["method"] == "initialize":
            response.headers["mcp-session-id"] = "synthetic-session"
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("repeat", [False, True])
async def test_background_refusal_preserves_cancel_and_joins_cleanup(
    monkeypatch: pytest.MonkeyPatch, kind: str, repeat: bool
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = StatefulFixture(kind, phase="sse", hold_cleanup=repeat)
    async with fixture.client() as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.started.wait(), 1)
            await asyncio.wait_for(fixture.refusal_closed.wait(), 1)
            assert remote._transport is not None and remote._transport.failure == "denied"
            task.cancel("original caller cancellation")
            if repeat:
                await asyncio.wait_for(fixture.closing.wait(), 1)
                task.cancel("second caller cancellation")
                await asyncio.sleep(0)
                assert not task.done()
                assert not fixture.pool_closed.is_set()
                fixture.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.args == ("original caller cancellation",)
            assert task.cancelled()
            assert fixture.stopped.is_set()
            assert fixture.pool_closed.is_set()
        finally:
            fixture.release.set()
            fixture.cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert fixture.pool_closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("code", ["denied", "protocol"])
async def test_terminal_failure_drains_only_its_client(
    monkeypatch: pytest.MonkeyPatch, kind: str, code: str
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    failed, neighbor = HTTPFixture(kind, phase="sse"), HTTPFixture(kind, phase="sse")
    revoked = False

    async def authorization() -> None:
        if revoked:
            if code == "denied":
                raise source.MCPSourceError("denied")
            raise RuntimeError("private upstream diagnostics")

    async with (
        source.MCPSourceClient(
            failed.config(),
            None,
            validate_access=authorization,
            _transport=FixtureTransport(failed),
        ) as first,
        neighbor.client() as second,
    ):
        first_task = asyncio.create_task(first.read(IDENTITY))
        second_task = asyncio.create_task(second.read(IDENTITY))
        try:
            await asyncio.wait_for(failed.started.wait(), 1)
            await asyncio.wait_for(neighbor.started.wait(), 1)
            revoked = True
            with pytest.raises(source.MCPSourceError) as refusal:
                await first.discover()
            assert refusal.value.code == code
            assert str(refusal.value) == "MCP source " + code
            assert failed.stopped.is_set()
            assert failed.pool_closed.is_set()
            assert not neighbor.stopped.is_set()
            assert not neighbor.pool_closed.is_set()
            assert not second_task.done()
            neighbor.release.set()
            evidence = await second_task
            assert evidence.text == TEXT
            assert evidence.receipt is not None
            outcome = (await asyncio.gather(first_task, return_exceptions=True))[0]
            assert isinstance(outcome, source.MCPSourceError)
        finally:
            failed.release.set()
            neighbor.release.set()
            for task in (first_task, second_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first_task, second_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("cancel_cleanup", [False, True])
async def test_request_deadline_waits_for_finalizer_despite_repeated_cancel(
    monkeypatch: pytest.MonkeyPatch, kind: str, cancel_cleanup: bool
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HeartbeatFixture(kind, phase="sse", hold_cleanup=True)
    config = fixture.config()
    config = replace(config, limits=replace(config.limits, request_seconds=0.15))
    async with source.MCPSourceClient(
        config, None, validate_access=access, _transport=FixtureTransport(fixture)
    ) as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.closing.wait(), 1)
            assert fixture.beats >= 2
            assert not task.done()
            assert not fixture.pool_closed.is_set()
            if cancel_cleanup:
                task.cancel("cleanup caller cancellation")
                await asyncio.sleep(0)
                task.cancel("repeated cleanup cancellation")
                await asyncio.sleep(0)
                assert not task.done()
            fixture.cleanup_release.set()
            if cancel_cleanup:
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await task
                assert cancelled.value.args == ("cleanup caller cancellation",)
                assert task.cancelled()
            else:
                with pytest.raises(source.MCPSourceError, match="timeout"):
                    await task
            assert fixture.stopped.is_set()
            assert fixture.pool_closed.is_set()
        finally:
            fixture.release.set()
            fixture.cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert fixture.pool_closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_total_deadline_drains_active_http_before_return(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    monkeypatch.setattr(loop, "time", clock.monotonic)
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture(kind, phase="sse")
    config = fixture.config()
    config = replace(config, limits=replace(config.limits, request_seconds=10))
    async with source.MCPSourceClient(
        config, None, validate_access=access, _transport=FixtureTransport(fixture)
    ) as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.started.wait(), 1)
            assert not task.done()
            clock.advance(6)
            with pytest.raises(source.MCPSourceError, match="timeout"):
                await task
            assert fixture.stopped.is_set()
            assert fixture.pool_closed.is_set()
            assert fixture.reads == 1
        finally:
            fixture.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert fixture.pool_closes == 1


@dataclass
class HandoffFixture(HTTPFixture):
    acquired: asyncio.Event = field(default_factory=asyncio.Event)
    checking: asyncio.Event = field(default_factory=asyncio.Event)
    authority_release: asyncio.Event = field(default_factory=asyncio.Event)
    private_failure: bool = False
    response_closes: int = 0

    async def reply(self, request: httpx.Request) -> httpx.Response:
        response = await super().reply(request)
        if request.method == "POST":
            method = json.loads(await request.aread())["method"]
            if method in {"resources/read", "tools/call"}:
                self.acquired.set()
        return response

    async def authority(self) -> None:
        if self.acquired.is_set():
            self.checking.set()
            if self.private_failure:
                raise RuntimeError("private callback details")
            await self.authority_release.wait()

    async def cleanup(self) -> None:
        self.response_closes += 1
        await super().cleanup()

    def client(self) -> source.MCPSourceClient:
        return source.MCPSourceClient(
            self.config(), None, validate_access=self.authority, _transport=FixtureTransport(self)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("repeat", [False, True])
async def test_response_handoff_cancel_awaits_acquired_finalizer(
    monkeypatch: pytest.MonkeyPatch, kind: str, repeat: bool
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HandoffFixture(kind, block=False, phase="sse", hold_cleanup=repeat)
    async with fixture.client() as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.checking.wait(), 1)
            task.cancel("original handoff cancellation")
            if repeat:
                await asyncio.wait_for(fixture.closing.wait(), 1)
                assert not task.done()
                task.cancel("repeated handoff cancellation")
                await asyncio.sleep(0)
                assert not task.done()
                assert not fixture.pool_closed.is_set()
                fixture.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.args == ("original handoff cancellation",)
            assert task.cancelled()
            assert fixture.stopped.is_set(), "acquired response finalizer was abandoned"
            assert fixture.response_closes == 1
            assert fixture.pool_closed.is_set()
            assert not fixture.started.is_set(), "unapproved response reached body iteration"
        finally:
            fixture.cleanup_release.set()
            fixture.authority_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("deadline", ["request", "total"])
async def test_response_handoff_deadline_awaits_acquired_finalizer(
    monkeypatch: pytest.MonkeyPatch, kind: str, deadline: str
) -> None:
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    monkeypatch.setattr(loop, "time", clock.monotonic)
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HandoffFixture(kind, block=False, phase="sse")
    config = fixture.config()
    config = replace(
        config, limits=replace(config.limits, request_seconds=10 if deadline == "total" else 2)
    )
    async with source.MCPSourceClient(
        config, None, validate_access=fixture.authority, _transport=FixtureTransport(fixture)
    ) as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.checking.wait(), 1)
            clock.advance(6 if deadline == "total" else 3)
            with pytest.raises(source.MCPSourceError, match="timeout"):
                await task
            assert fixture.stopped.is_set(), "deadline abandoned the acquired response"
            assert fixture.response_closes == 1
            assert fixture.pool_closed.is_set()
            assert not fixture.started.is_set()
        finally:
            fixture.authority_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_response_handoff_private_failure_keeps_neighbor_independent(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HandoffFixture(
        kind, block=False, phase="sse", private_failure=True, hold_cleanup=True
    )
    neighbor = HTTPFixture(kind, phase="sse")
    async with fixture.client() as first, neighbor.client() as second:
        other = asyncio.create_task(second.read(IDENTITY))
        failed = asyncio.create_task(first.read(IDENTITY))
        try:
            await asyncio.wait_for(neighbor.started.wait(), 1)
            await asyncio.wait_for(fixture.closing.wait(), 1)
            assert not failed.done()
            assert not neighbor.pool_closed.is_set()
            assert not other.done()
            neighbor.release.set()
            evidence = await other
            assert evidence.text == TEXT
            assert evidence.receipt is not None
            fixture.cleanup_release.set()
            with pytest.raises(source.MCPSourceError) as refusal:
                await failed
            assert str(refusal.value) == "MCP source protocol"
            assert fixture.stopped.is_set()
            assert fixture.response_closes == 1
            assert fixture.pool_closed.is_set()
        finally:
            fixture.cleanup_release.set()
            neighbor.release.set()
            for task in (other, failed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(other, failed, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_response_handoff_authorized_neighbor_returns_receipt(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HandoffFixture(kind, block=False, phase="sse")
    async with fixture.client() as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.checking.wait(), 1)
            assert fixture.response_closes == 0
            fixture.authority_release.set()
            evidence = await task
            assert evidence.text == TEXT
            assert evidence.receipt is not None
            assert fixture.response_closes == 1
        finally:
            fixture.authority_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_response_handoff_cancel_finalizer_outlives_total_deadline(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    monkeypatch.setattr(loop, "time", clock.monotonic)
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HandoffFixture(kind, block=False, phase="sse", hold_cleanup=True)
    async with fixture.client() as remote:
        task = asyncio.create_task(remote.read(IDENTITY))
        try:
            await asyncio.wait_for(fixture.checking.wait(), 1)
            task.cancel("original handoff cancellation")
            await asyncio.wait_for(fixture.closing.wait(), 1)
            clock.advance(6)
            # Deliver all expired deadline callbacks while the finalizer is blocked.
            for _ in range(100):
                await asyncio.sleep(0)
                if task.done():
                    break
            assert not task.done(), "the deadline interrupted acquired-response cleanup"
            assert not fixture.pool_closed.is_set()
            fixture.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.args == ("original handoff cancellation",)
            assert fixture.stopped.is_set()
            assert fixture.response_closes == 1
            assert fixture.pool_closed.is_set()
        finally:
            fixture.cleanup_release.set()
            fixture.authority_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_state_guard_background_denial_refuses_receipt_without_caller_cancel(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture, neighbor = StatefulFixture(kind, phase="sse"), HTTPFixture(kind, phase="sse")
    async with fixture.client() as first, neighbor.client() as second:
        failed, other = (
            asyncio.create_task(first.read(IDENTITY)),
            asyncio.create_task(second.read(IDENTITY)),
        )
        try:
            await asyncio.wait_for(fixture.started.wait(), 1)
            await asyncio.wait_for(neighbor.started.wait(), 1)
            await asyncio.wait_for(fixture.refusal_closed.wait(), 1)
            assert first._transport is not None and first._transport.failure == "denied"
            assert not neighbor.pool_closed.is_set() and not other.done()
            fixture.release.set()
            neighbor.release.set()
            evidence = await other
            assert evidence.receipt is not None and evidence.text == TEXT
            with pytest.raises(source.MCPSourceError, match="denied"):
                await failed
            assert fixture.stopped.is_set() and fixture.pool_closed.is_set()
            reads = fixture.reads
            with pytest.raises(source.MCPSourceError, match="denied"):
                await first.read(IDENTITY)
            assert fixture.reads == reads
        finally:
            fixture.release.set()
            neighbor.release.set()
            for task in (failed, other):
                if not task.done():
                    task.cancel()
            await asyncio.gather(failed, other, return_exceptions=True)


class HeldRefusalStream(RefusalStream):
    def __init__(self, closed: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(closed)
        self.release = release

    async def aclose(self) -> None:
        self.closed.set()
        await self.release.wait()


@dataclass
class StateRaceFixture(HTTPFixture):
    pause_at: str = "handoff"
    hold_refusal: bool = False
    refusal_release: asyncio.Event = field(default_factory=asyncio.Event)
    armed: bool = False
    acquired: bool = False
    sdk_checks: int = 0
    caller: asyncio.Task[Any] | None = None
    checking: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    denied: asyncio.Event = field(default_factory=asyncio.Event)
    initialized: asyncio.Event = field(default_factory=asyncio.Event)
    get_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def reply(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.get_started.set()
            await self.checking.wait()
            stream = (
                HeldRefusalStream(self.denied, self.refusal_release)
                if self.hold_refusal
                else RefusalStream(self.denied)
            )
            return httpx.Response(403, stream=stream)
        if request.method == "DELETE":
            return httpx.Response(204)
        response = await super().reply(request)
        method = json.loads(await request.aread())["method"]
        if method == "initialize":
            response.headers["mcp-session-id"] = "synthetic-session"
        elif method == "notifications/initialized":
            self.initialized.set()
        elif method in {"resources/read", "tools/call"}:
            self.acquired = True
        return response

    async def authority(self) -> None:
        if not self.armed or self.checking.is_set():
            return
        caller = asyncio.current_task() is self.caller
        if not caller and not self.acquired:
            self.sdk_checks += 1
        matches = {
            "operation": caller and not self.acquired,
            "dispatch": not caller and not self.acquired and self.sdk_checks == 2,
            "handoff": self.acquired and not self.started.is_set(),
            "chunk": self.started.is_set() and not self.stopped.is_set(),
            "publication": caller and self.stopped.is_set(),
        }
        if matches[self.pause_at]:
            self.checking.set()
            await self.resume.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
@pytest.mark.parametrize("phase", ["operation", "dispatch", "handoff", "chunk", "publication"])
async def test_state_guard_rechecks_denial_after_suspended_authority(
    monkeypatch: pytest.MonkeyPatch, kind: str, phase: str
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = StateRaceFixture(kind, block=False, phase="sse", pause_at=phase)
    config = fixture.config()
    config = replace(config, limits=replace(config.limits, request_seconds=0.3))
    async with source.MCPSourceClient(
        config, None, validate_access=fixture.authority, _transport=FixtureTransport(fixture)
    ) as remote:
        await asyncio.wait_for(fixture.initialized.wait(), 1)
        await asyncio.wait_for(fixture.get_started.wait(), 1)
        fixture.armed = True
        requests_before = fixture.requests
        task = asyncio.create_task(remote.read(IDENTITY))
        fixture.caller = task
        try:
            await asyncio.wait_for(fixture.checking.wait(), 1)
            await asyncio.wait_for(fixture.denied.wait(), 1)
            assert remote._transport is not None and remote._transport.failure == "denied"
            fixture.resume.set()
            with pytest.raises(source.MCPSourceError, match="denied"):
                await task
            assert fixture.pool_closed.is_set()
            if phase in {"operation", "dispatch"}:
                assert fixture.reads == 0
                assert fixture.requests == requests_before
            else:
                assert fixture.stopped.is_set()
            if phase == "handoff":
                assert not fixture.started.is_set()
            reads = fixture.reads
            with pytest.raises(source.MCPSourceError, match="denied"):
                await remote.read(IDENTITY)
            assert fixture.reads == reads
        finally:
            fixture.resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "authority", "publication"])
async def test_state_guard_synchronous_discovery_refuses_elapsed_deadline(
    monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    monkeypatch.setattr(loop, "time", clock.monotonic)
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture("resources")
    armed = False
    checks = 0

    async def authority() -> None:
        nonlocal checks
        if armed:
            checks += 1
            if when != "publication" or checks == 2:
                clock.advance(6)

    async with source.MCPSourceClient(
        fixture.config(), None, validate_access=authority, _transport=FixtureTransport(fixture)
    ) as remote:
        before = fixture.requests
        if when == "before":
            clock.advance(6)
        else:
            armed = True
        with pytest.raises(source.MCPSourceError, match="timeout"):
            await remote.discover()
        assert fixture.pool_closed.is_set()
        assert fixture.requests == before


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["denied", "limit", "protocol"])
async def test_state_guard_cached_publication_rechecks_terminal_failure(
    monkeypatch: pytest.MonkeyPatch, code: source.ErrorCode
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture("resources")
    checks = 0
    armed = False

    async def authority() -> None:
        nonlocal checks
        if armed:
            checks += 1
            if checks == 2:
                assert remote._transport is not None
                remote._transport.failure = code

    async with source.MCPSourceClient(
        fixture.config(), None, validate_access=authority, _transport=FixtureTransport(fixture)
    ) as remote:
        armed = True
        with pytest.raises(source.MCPSourceError, match=code):
            await remote.discover()
        assert fixture.pool_closed.is_set()
        assert fixture.reads == 0


@pytest.mark.asyncio
async def test_state_guard_closing_owner_cannot_publish_cached_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = HTTPFixture("resources")
    checks = 0
    armed = False

    async def authority() -> None:
        nonlocal checks
        if armed:
            checks += 1
            if checks == 2:
                await remote._close()

    async with source.MCPSourceClient(
        fixture.config(), None, validate_access=authority, _transport=FixtureTransport(fixture)
    ) as remote:
        armed = True
        with pytest.raises(source.MCPSourceError, match="closed"):
            await remote.discover()
        assert fixture.pool_closed.is_set()
        assert fixture.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_state_guard_caller_cancel_keeps_identity_after_background_denial(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = StateRaceFixture(kind, block=False, phase="sse", hold_cleanup=True)
    async with source.MCPSourceClient(
        fixture.config(),
        None,
        validate_access=fixture.authority,
        _transport=FixtureTransport(fixture),
    ) as remote:
        await asyncio.wait_for(fixture.initialized.wait(), 1)
        await asyncio.wait_for(fixture.get_started.wait(), 1)
        fixture.armed = True
        task = asyncio.create_task(remote.read(IDENTITY))
        fixture.caller = task
        try:
            await asyncio.wait_for(fixture.checking.wait(), 1)
            await asyncio.wait_for(fixture.denied.wait(), 1)
            task.cancel("original state-guard cancellation")
            await asyncio.wait_for(fixture.closing.wait(), 1)
            task.cancel("repeated state-guard cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            fixture.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.args == ("original state-guard cancellation",)
            assert fixture.stopped.is_set() and fixture.pool_closed.is_set()
        finally:
            fixture.resume.set()
            fixture.cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_state_guard_ordinary_stateful_session_still_returns_receipt_and_closes(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = StateRaceFixture(kind, block=False, phase="sse")
    async with source.MCPSourceClient(
        fixture.config(),
        None,
        validate_access=fixture.authority,
        _transport=FixtureTransport(fixture),
    ) as remote:
        await asyncio.wait_for(fixture.initialized.wait(), 1)
        await asyncio.wait_for(fixture.get_started.wait(), 1)
        evidence = await remote.read(IDENTITY)
        assert evidence.receipt is not None and evidence.text == TEXT
        assert fixture.stopped.is_set()
        assert not fixture.pool_closed.is_set()
    assert fixture.pool_closed.is_set() and fixture.pool_closes == 1
    assert remote._transport is not None and remote._transport.failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resources", "tools"])
async def test_state_guard_refusal_poison_precedes_held_response_cleanup(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    monkeypatch.setattr(loop, "time", clock.monotonic)
    monkeypatch.setattr(source, "pinned_target", pin)
    fixture = StateRaceFixture(kind, block=False, phase="sse", pause_at="chunk", hold_refusal=True)
    config = fixture.config()
    config = replace(config, limits=replace(config.limits, request_seconds=0.3))
    async with source.MCPSourceClient(
        config, None, validate_access=fixture.authority, _transport=FixtureTransport(fixture)
    ) as remote:
        await asyncio.wait_for(fixture.initialized.wait(), 1)
        await asyncio.wait_for(fixture.get_started.wait(), 1)
        fixture.armed = True
        task = asyncio.create_task(remote.read(IDENTITY))
        fixture.caller = task
        try:
            await asyncio.wait_for(fixture.denied.wait(), 1)
            assert remote._transport is not None and remote._transport.failure == "denied"
            fixture.resume.set()
            await asyncio.wait_for(fixture.stopped.wait(), 1)
            clock.advance(0.4)
            for _ in range(100):
                await asyncio.sleep(0)
                if remote._closing:
                    break
            assert remote._closing and not task.done()
            assert not fixture.pool_closed.is_set()
            fixture.refusal_release.set()
            with pytest.raises(source.MCPSourceError, match="denied"):
                await task
            assert fixture.pool_closed.is_set()
        finally:
            fixture.resume.set()
            fixture.refusal_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
