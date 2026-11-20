"""Source checkouts remain bound to standing, destination and one bounded response."""

from __future__ import annotations

import asyncio
import copy
import pickle
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import pytest

from server.clients import _checked_access, _vendor, linear, notion
from server.clients._checked_access import CheckedAccess
from tests.unit.clock_fake import ManualClock

_TOKEN = "private-checkout-token"
_ID = "12345678-1234-1234-1234-123456789abc"
_MODES = ("json", "download", "notion", "linear")


class _StandingLost(RuntimeError):
    pass


class _Stream(httpx.AsyncByteStream):
    def __init__(
        self,
        pieces: list[bytes],
        events: list[str],
        *,
        before_read: Callable[[], None] | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.pieces = pieces
        self.events = events
        self.before_read = before_read
        self.gate = gate
        self.entered = asyncio.Event()
        self.closed = False
        self.read_count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        if self.before_read is not None:
            self.before_read()
        if self.gate is not None:
            await self.gate.wait()
        for piece in self.pieces:
            self.read_count += 1
            self.events.append("chunk")
            yield piece

    async def aclose(self) -> None:
        self.closed = True
        self.events.append("closed")


def _checkout(
    mode: str, validate: Callable[[], Awaitable[None]], *, deadline: float | None = None
) -> CheckedAccess:
    kind: Literal["gdrive", "notion", "linear"] = (
        "linear" if mode == "linear" else "notion" if mode == "notion" else "gdrive"
    )
    return CheckedAccess(
        _TOKEN,
        kind=kind,
        deadline=time.monotonic() + 60 if deadline is None else deadline,
        validate=validate,
    )


def _body(mode: str) -> bytes:
    if mode == "download":
        return b"visible"
    if mode == "linear":
        return b'{"data":{"document":{"id":"visible"}}}'
    return b'{"id":"visible"}'


async def _fetch(
    mode: str, access: CheckedAccess, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> Any:
    if mode == "download":
        return await _vendor.get_capped(
            client,
            "https://www.googleapis.com/drive/v3/files/file",
            access,
            "download",
            max_bytes=64,
        )
    if mode == "notion":
        monkeypatch.setattr(notion, "get_client", lambda: client)
        monkeypatch.setattr(notion, "_next_request_at", {})
        return await notion.page(access, _ID)
    if mode == "linear":
        monkeypatch.setattr(linear, "get_client", lambda: client)
        return await linear.document(access, _ID, max_bytes=64)
    return await _vendor.bearer_get(
        client, "https://www.googleapis.com/drive/v3/files/file", access, "metadata"
    )


@pytest.mark.parametrize("mode", _MODES)
async def test_standing_is_checked_before_dispatch_and_after_the_stream_closes(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    stream = _Stream([_body(mode)], events)

    async def validate() -> None:
        events.append("check")
        if events.count("check") == 2:
            assert stream.closed

    def handle(request: httpx.Request) -> httpx.Response:
        events.append("dispatch")
        assert request.headers["Authorization"] == "Bearer " + _TOKEN
        expected_url = {
            "json": "https://www.googleapis.com/drive/v3/files/file",
            "download": "https://www.googleapis.com/drive/v3/files/file",
            "notion": f"https://api.notion.com/v1/pages/{_ID}",
            "linear": "https://api.linear.app/graphql",
        }[mode]
        assert str(request.url) == expected_url
        assert request.method == ("POST" if mode == "linear" else "GET")
        if mode == "notion":
            assert request.headers["Notion-Version"] == notion.API_VERSION
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        answer = await _fetch(mode, _checkout(mode, validate), client, monkeypatch)
    assert answer == (b"visible" if mode == "download" else {"id": "visible"})
    assert events == ["check", "dispatch", "chunk", "closed", "check"]


@pytest.mark.parametrize("mode", _MODES)
async def test_standing_lost_while_reading_withholds_the_completed_response(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    standing = True
    events: list[str] = []

    def revoke() -> None:
        nonlocal standing
        standing = False

    stream = _Stream([_body(mode)], events, before_read=revoke)

    async def validate() -> None:
        events.append("check")
        if not standing:
            assert stream.closed
            raise _StandingLost("source unavailable")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(_StandingLost, match="^source unavailable$"):
            await _fetch(mode, _checkout(mode, validate), client, monkeypatch)
    assert events == ["check", "chunk", "closed", "check"]


async def test_a_checkout_refused_before_dispatch_sends_no_request() -> None:
    requests: list[httpx.Request] = []

    async def validate() -> None:
        raise _StandingLost("source unavailable")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(_StandingLost, match="^source unavailable$"):
            await _vendor.bearer_get(
                client,
                "https://www.googleapis.com/drive/v3/files/file",
                _checkout("json", validate),
                "metadata",
            )
    assert requests == []


@pytest.mark.parametrize("mode", ("json", "download"))
async def test_body_bounds_stop_reading_and_close_before_refusing(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    stream = _Stream([b"x" * 65, b"must-not-be-read"], events)
    monkeypatch.setattr(_vendor.settings, "knowledge_source_sync_max_file_bytes", 64)

    async def validate() -> None:
        events.append("check")
        if events.count("check") == 2:
            assert stream.closed

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(_vendor.TooLarge):
            await _fetch(mode, _checkout(mode, validate), client, monkeypatch)
    assert stream.read_count == 1
    assert events == ["check", "chunk", "closed", "check"]


@pytest.mark.parametrize("mode", _MODES)
async def test_cancellation_closes_the_body_without_releasing_content(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    stream = _Stream([_body(mode)], events, gate=asyncio.Event())

    async def validate() -> None:
        events.append("check")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        work = asyncio.create_task(_fetch(mode, _checkout(mode, validate), client, monkeypatch))
        entered = asyncio.create_task(stream.entered.wait())
        try:
            done, _ = await asyncio.wait({work, entered}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
                pytest.fail("the blocked response was released")
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
        finally:
            for task in (work, entered):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, entered, return_exceptions=True)
    assert stream.closed
    assert stream.read_count == 0
    assert events == ["check", "closed"]


@pytest.mark.parametrize("mode", _MODES)
async def test_redirects_never_forward_the_checkout_even_if_the_client_follows(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    stream = _Stream([_body(mode)], [])

    async def validate() -> None:
        pass

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302, headers={"Location": "https://untrusted.example/collect"}, stream=stream
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        with pytest.raises((_vendor.VendorError, linear.LinearError)) as caught:
            await _fetch(mode, _checkout(mode, validate), client, monkeypatch)
        assert caught.value.status == 302
    assert stream.closed
    assert len(requests) == 1
    assert requests[0].url.host != "untrusted.example"
    assert requests[0].headers["Authorization"] == "Bearer " + _TOKEN


async def test_destination_lookalikes_and_normalized_path_escapes_are_refused() -> None:
    checks = 0

    async def validate() -> None:
        nonlocal checks
        checks += 1

    access = _checkout("json", validate)
    for url in (
        "http://www.googleapis.com/drive/v3/files/file",
        "https://www.googleapis.com.evil.example/drive/v3/files/file",
        "https://user@www.googleapis.com/drive/v3/files/file",
        "https://www.googleapis.com:443/drive/v3/files/file",
        "https://www.googleapis.com/drive/v3/files/file#private",
        "https://www.googleapis.com/drive/v30/files/file",
        "https://api.notion.com/v1/pages/page",
        "https://www.googleapis.com/drive/v3/../../oauth2/v2/userinfo",
        "https://www.googleapis.com/drive/v3/%2e%2e/files/file",
        "https://www.googleapis.com/drive/v3/files\\private",
        "https://www.googleapis.com/drive/v3/files/file?access_token=other",
    ):
        with pytest.raises(ValueError, match="^connected checkout destination refused$"):
            await access.authorization(url)
    assert checks == 0


async def test_expiry_during_validation_refuses_the_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(t=time.monotonic())
    monkeypatch.setattr(_checked_access, "time", SimpleNamespace(monotonic=clock.monotonic))

    async def validate() -> None:
        clock.advance(61)

    access = _checkout("json", validate, deadline=clock.monotonic() + 60)
    with pytest.raises(TimeoutError, match="^connected checkout expired$"):
        await access.authorization("https://www.googleapis.com/drive/v3/files/file")


async def test_the_deadline_bounds_a_blocked_validator_and_response_body() -> None:
    async def blocked_validate() -> None:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await _checkout("json", blocked_validate, deadline=time.monotonic() + 1).check()

    async def validate() -> None:
        pass

    stream = _Stream([b"private"], [], gate=asyncio.Event())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        access = _checkout("download", validate, deadline=time.monotonic() + 1)
        with pytest.raises(TimeoutError):
            await _vendor.get_capped(
                client,
                "https://www.googleapis.com/drive/v3/files/file",
                access,
                "download",
                max_bytes=64,
            )
    assert stream.entered.is_set()
    assert stream.closed
    assert stream.read_count == 0


def test_a_checkout_cannot_be_converted_or_serialized_as_a_shared_credential() -> None:
    async def validate() -> None:
        pass

    access = _checkout("json", validate)
    assert _TOKEN not in repr(access)
    assert _TOKEN not in access.pacing_key()
    for convert in (str, copy.copy, pickle.dumps):
        with pytest.raises(TypeError):
            convert(access)


async def test_project_acl_bound_refuses_a_partial_permission_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def validate() -> None:
        pass

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "project": {
                        "teams": {
                            "nodes": [{"id": _ID}, {"id": "22345678-1234-1234-1234-123456789abc"}],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            },
        )

    monkeypatch.setattr(_vendor.settings, "knowledge_source_acl_max_entries", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        with pytest.raises(linear.TooLarge):
            await linear.project_member_emails(_checkout("linear", validate), _ID)
    assert len(calls) == 1
