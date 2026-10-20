"""``GET /v1/task/watch`` (wake-up layer): the SSE framing
over a fake cursor read, the cursor rules, and the route's headers. The
stream is a hint over ``task_events``; the read is the source of truth."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.tasks.app import watch_routes as w


def _row(seq: int, event: str = "created") -> dict[str, Any]:
    return {
        "seq": seq,
        "task_id": "00000000-0000-0000-0000-000000000001",
        "inbox": "bot",
        "event": event,
        "from_status": "",
        "to_status": "todo",
        "actor": "process:sa:public-bot",
        "agent": "public-bot",
        "from_subject": "sa:public-bot",
        "fence": 0,
        "version": seq,
        "reason": "",
        "patch": json.dumps({"kind": "k"}),
        "note": "",
        "ts": datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    }


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def _collect(gen) -> list[str]:
    return [frame async for frame in gen]


def test_frame_event_carries_the_seq_as_id_and_the_payload_as_json() -> None:
    frame = w.frame_event(_row(42))
    assert frame.startswith("id: 42\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["seq"] == 42 and payload["event"] == "created"
    assert payload["patch"] == {"kind": "k"} and payload["to_status"] == "todo"
    assert "from_subject" not in payload  # the payload is the wire shape task.updates uses


@pytest.mark.asyncio
async def test_stream_replays_after_the_cursor_then_waits_and_ends_at_the_bound() -> None:
    clock = FakeClock()
    reads: list[int] = []
    waited: list[tuple[str, float]] = []
    batches = [[_row(5), _row(7)], [], [_row(9)], []]

    async def read(since: int) -> list[dict[str, Any]]:
        reads.append(since)
        return batches.pop(0) if batches else []

    async def wait(key: str, seconds: float) -> float:
        waited.append((key, seconds))
        clock.t += 2.0  # the helper's re-read window
        return 2.0

    frames = await _collect(
        w.event_stream(
            key="inbox:acme:bot",
            since_seq=4,
            heartbeat_seconds=15,
            max_seconds=5,
            read=read,
            wait=wait,
            clock=clock,
        )
    )
    assert frames[0] == "retry: 3000\n\n"
    ids = [int(f.split("\n", 1)[0][4:]) for f in frames[1:] if f.startswith("id: ")]
    assert ids == [5, 7, 9]
    # each read starts after the last seq it saw
    assert reads[:3] == [4, 7, 7] and reads[3] == 9
    assert all(k == "inbox:acme:bot" for k, _ in waited)
    # the bound ended the stream: no frame after the deadline, no error
    assert clock.t >= 105


@pytest.mark.asyncio
async def test_stream_sends_a_keepalive_comment_on_the_heartbeat() -> None:
    clock = FakeClock()

    async def read(since: int) -> list[dict[str, Any]]:
        return []

    async def wait(key: str, seconds: float) -> float:
        clock.t += seconds
        return seconds

    frames = await _collect(
        w.event_stream(
            key="poster:acme:sa:bot",
            since_seq=0,
            heartbeat_seconds=3,
            max_seconds=7,
            read=read,
            wait=wait,
            clock=clock,
        )
    )
    assert frames[0] == "retry: 3000\n\n"
    assert frames.count(": keepalive\n\n") == 2  # at t+3 and t+6, then the bound at t+7


@pytest.mark.asyncio
async def test_a_stream_that_keeps_receiving_rows_still_ends_at_the_bound() -> None:
    """Every read returns rows: the stream never reaches its wait, yet max_seconds still
    ends it (the client resumes from its cursor)."""
    clock = FakeClock()
    seq = [0]

    async def read(since: int) -> list[dict[str, Any]]:
        clock.t += 1.0
        seq[0] += 1
        return [_row(seq[0])]

    async def wait(key: str, seconds: float) -> float:
        raise AssertionError("a busy stream never waits")

    frames = await _collect(
        w.event_stream(
            key="inbox:acme:bot",
            since_seq=0,
            heartbeat_seconds=15,
            max_seconds=5,
            read=read,
            wait=wait,
            clock=clock,
        )
    )
    assert len([f for f in frames if f.startswith("id: ")]) == 5


def test_cursor_prefers_last_event_id_and_clamps_the_bounds() -> None:
    from starlette.requests import Request

    def req(headers: dict[str, str]) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "method": "GET", "path": "/"})

    assert w._cursor_from(req({"Last-Event-ID": "77"}), 5) == 77
    assert w._cursor_from(req({"Last-Event-ID": "nope"}), 5) == 5
    assert w._cursor_from(req({}), -3) == 0
    assert w._clamp(0, low=1, high=60) == 1
    assert w._clamp(9_999, low=1, high=w.MAX_MAX_SECONDS) == 600


@pytest.mark.asyncio
async def test_route_streams_with_no_buffering_headers_and_a_404_for_a_missing_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.tasks.store import repo as repo

    # auth off: the route's bearer check is the knowledge.download rule and
    # row_in_scope answers True with no principal
    monkeypatch.setattr(w, "settings", SimpleNamespace(auth_actually_required=lambda: False))

    inboxes = {("acme", "bot"): {"org": "acme", "inbox": "bot", "owner_namespace": "ns"}}

    async def fetch_inbox(org: str, inbox: str) -> dict[str, Any] | None:
        return inboxes.get((org, inbox))

    calls: list[int] = []

    async def events_since(org: str, **kw: Any) -> list[dict[str, Any]]:
        calls.append(kw["since_seq"])
        assert kw["inbox"] == "bot" and kw["from_subject"] is None
        return [_row(11)] if len(calls) == 1 else []

    monkeypatch.setattr(repo, "fetch_inbox_row", fetch_inbox)
    monkeypatch.setattr(repo, "events_since", events_since)

    app = FastAPI()
    w.attach(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/task/watch", params={"org": "acme", "inbox": "nope"})
        assert r.status_code == 404
        r = await c.get("/v1/task/watch", params={"org": "acme"})
        assert r.status_code == 422
        async with c.stream(
            "GET",
            "/v1/task/watch",
            params={"org": "acme", "inbox": "bot", "max_seconds": 1, "heartbeat_seconds": 1},
            headers={"Last-Event-ID": "10"},
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-cache"
            assert r.headers["x-accel-buffering"] == "no"
            assert r.headers["content-encoding"] == "identity"
            body = "".join([chunk async for chunk in r.aiter_text()])
    assert body.startswith("retry: 3000\n\n")
    assert "id: 11\ndata: " in body
    assert calls[0] == 10  # Last-Event-ID was the cursor


@pytest.mark.asyncio
async def test_a_present_bearer_is_honoured_even_when_auth_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI runs the server with LOOM_AUTH_REQUIRED=false and still mints a
    stranger's token: the transport verifies a bearer that is present, and so
    does the route — the refusal is a plain 404/403 before any frame."""
    from types import SimpleNamespace

    from server.tasks.app import common as th
    from server.tasks.store import repo as repo

    monkeypatch.setattr(w, "settings", SimpleNamespace(auth_actually_required=lambda: False))
    principal = SimpleNamespace(
        org="acme",
        subject="eve",
        is_service_account=False,
        gateway=False,
        has_role=lambda role: False,
    )
    monkeypatch.setattr("server.auth.bearer.verify_token", lambda tok: principal)

    async def fetch_inbox(org: str, inbox: str) -> dict[str, Any] | None:
        return {"org": org, "inbox": inbox, "owner_namespace": "ns"}

    seen: list[tuple[str, str]] = []
    eve_reads_ns = False

    async def in_scope(org: str, namespace: str, *, write: bool, hides_answer=True) -> bool:
        from server.auth.context import current_auth

        seen.append((org, namespace))
        if current_auth() is None:  # no principal = auth off
            return True
        return eve_reads_ns and not write  # eve is a writer elsewhere, at most a reader here

    async def events_since(org: str, **kw: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(repo, "fetch_inbox_row", fetch_inbox)
    monkeypatch.setattr(repo, "events_since", events_since)
    monkeypatch.setattr(th, "_in_scope", in_scope)

    app = FastAPI()
    w.attach(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            "/v1/task/watch",
            params={"org": "acme", "inbox": "bot"},
            headers={"Authorization": "Bearer eve-token"},
        )
        # not a reader of the owner namespace: the unknown-inbox 404, naming nothing
        assert r.status_code == 404 and r.json()["detail"]["code"] == "RESOURCE_NOT_FOUND"
        assert seen == [("acme", "ns")]
        # a reader short of writer already sees the inbox: the plain 403
        eve_reads_ns = True
        r = await c.get(
            "/v1/task/watch",
            params={"org": "acme", "inbox": "bot"},
            headers={"Authorization": "Bearer eve-token"},
        )
        assert r.status_code == 403 and r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
        # another org's token is refused before the inbox is even read
        r = await c.get(
            "/v1/task/watch",
            params={"org": "other", "inbox": "bot"},
            headers={"Authorization": "Bearer eve-token"},
        )
        assert r.status_code == 403
        # no bearer, auth off: no principal, the stream opens
        async with c.stream(
            "GET", "/v1/task/watch", params={"org": "acme", "inbox": "bot", "max_seconds": 1}
        ) as r:
            assert r.status_code == 200
            await r.aclose()
