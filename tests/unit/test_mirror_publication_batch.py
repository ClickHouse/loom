"""A buffered batch never publishes a checked reply after a later entry waits."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from server.core import request_tail, request_work
from server.transport import mcp


def request(index, method="tools/call"):
    return {"jsonrpc": "2.0", "id": index, "method": method}


def frame(index, body):
    return {
        "jsonrpc": "2.0",
        "id": index,
        "result": {
            "content": [{"type": "text", "text": json.dumps(body)}],
            "structuredContent": body,
            "isError": False,
        },
    }


@pytest.fixture
def bag():
    tail = request_tail._Tail()
    token = request_tail._tail.set(tail)
    try:
        with request_work.http_owner({"type": "http"}):
            request_work.begin()
            yield SimpleNamespace(tail=tail)
    finally:
        request_tail._tail.reset(token)


@pytest.mark.parametrize("position", [0, 1, 2])
async def test_every_marked_batch_position_has_one_constant_refusal(bag, position):
    replies = []
    for body in (
        {"title": "CHECKED-TITLE", "content": "CHECKED-CONTENT", "cursor": "CHECKED-CURSOR"},
        {"results": [], "stale_dropped": 0},
        {"found": False},
    ):

        async def dispatch(entry, selected=body):
            index = entry["id"]
            request_work.begin()
            if index == position:
                assert request_work.mark_mirrored()
                return frame(index, selected)
            # In a real batch this could be a second provider or a write. It
            # runs after the earlier tool's last access check and before send.
            await asyncio.sleep(0)
            return frame(index, {"ordinary": index})

        output = await mcp._dispatch_batch([request(i) for i in range(3)], dispatch)
        assert [entry["id"] for entry in output] == [0, 1, 2]
        marked = output[position]
        assert "error" in marked or marked.get("result", {}).get("isError") is True
        assert "CHECKED-" not in json.dumps(marked)
        for i in range(3):
            if i != position:
                assert output[i] == frame(i, {"ordinary": i})
        replies.append(marked)
    assert replies[0] == replies[1] == replies[2]


async def test_one_entry_array_keeps_the_checked_answer(bag):
    expected = frame(1, {"content": "CHECKED-CONTENT"})

    async def dispatch(_entry):
        request_work.begin()
        assert request_work.mark_mirrored()
        return expected

    assert await mcp._dispatch_batch([request(1)], dispatch) == [expected]


async def test_a_later_early_return_does_not_inherit_the_previous_entry_mark(bag):
    seen = []

    async def dispatch(entry):
        if entry["method"] == "tools/call":
            request_work.begin()
            assert request_work.mark_mirrored()
            return frame(1, {"content": "CHECKED-CONTENT"})
        # A ping/invalid shape can return before the tool's begin_call runs.
        seen.append(request_work.mirror_marked())
        return {"jsonrpc": "2.0", "id": 2, "result": {}}

    output = await mcp._dispatch_batch([request(1), request(2, "ping")], dispatch)
    assert seen == [False]
    assert output[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert not request_work.mirror_marked()
    assert request_work.http_mirror_marked(), "the HTTP owner retains the batch mark"


async def test_a_later_ordinary_tool_cannot_clear_the_outer_batch_mark(bag):
    async def dispatch(entry):
        request_work.begin()
        if entry["id"] == 1:
            assert request_work.mark_mirrored()
        return frame(entry["id"], {"ordinary": entry["id"]})

    await mcp._dispatch_batch([request(1), request(2)], dispatch)
    assert not request_work.mirror_marked()
    assert request_work.http_mirror_marked()


async def test_zero_mirror_batch_keeps_frames_notifications_and_body_fields(bag):
    ordinary = frame(1, {"mirrored": True, "content": "a caller-controlled ordinary field"})
    refusal = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32600, "message": "invalid"}}

    async def dispatch(entry):
        if "id" not in entry:
            return None
        return ordinary if entry["id"] == 1 else refusal

    output = await mcp._dispatch_batch(
        [request(1), {"jsonrpc": "2.0", "method": "notifications/initialized"}, request(3)],
        dispatch,
    )
    assert output == [ordinary, refusal]
    assert not request_work.mirror_marked()


async def test_marked_refusal_is_identical_across_batch_positions(bag):
    replies = []
    for position in range(3):

        async def dispatch(entry, selected=position):
            request_work.begin()
            if entry["id"] == selected:
                assert request_work.mark_mirrored()
            return frame(entry["id"], {"content": "CHECKED-CONTENT"})

        output = await mcp._dispatch_batch([request(i) for i in range(3)], dispatch)
        marked = {**output[position], "id": "same-id"}
        assert "error" in marked or marked.get("result", {}).get("isError") is True
        assert "CHECKED-CONTENT" not in json.dumps(marked)
        replies.append(marked)
    assert replies[0] == replies[1] == replies[2]


@pytest.mark.parametrize("notification", [False, True])
async def test_required_entry_commit_precedes_the_next_call_without_ordinary_cleanup(
    bag, notification
):
    committed, ordinary = [], []

    async def commit(index):
        committed.append(index)

    async def later():
        ordinary.append("ordinary")

    async def dispatch(entry):
        index = entry.get("id", 1)
        if index == 2:
            assert committed == [1]
            assert ordinary == [] and request_tail.required_active()
            assert not request_work.mirror_marked()
        assert request_work.mark_mirrored()
        assert request_tail.defer_required(lambda: commit(index))
        if index == 1:
            assert request_tail.defer(later)
        return frame(index, {"content": "CHECKED-CONTENT"}) if "id" in entry else None

    first = request(1)
    if notification:
        first.pop("id")
    result = await mcp._dispatch_batch([first, request(2)], dispatch)
    assert committed == [1, 2] and ordinary == []
    assert request_tail.required_active() and not bag.tail.closed
    assert len(result) == (1 if notification else 2)
    assert all("CHECKED-CONTENT" not in json.dumps(reply) for reply in result)
    await request_tail.settle(bag.tail)
    await request_tail.settle(bag.tail)
    assert committed == [1, 2] and ordinary == ["ordinary"]


async def test_required_entry_failure_prevents_the_next_dispatch_and_stays_fatal(bag):
    visited, attempts, ordinary = [], [], []

    async def broken():
        attempts.append("required")
        raise RuntimeError("private insert detail")

    async def cleanup():
        ordinary.append("ordinary")

    async def dispatch(entry):
        visited.append(entry["id"])
        if entry["id"] == 1:
            assert request_work.mark_mirrored()
            assert request_tail.defer_required(broken)
            assert request_tail.defer(cleanup)
        return frame(entry["id"], {"content": "CHECKED-CONTENT"})

    with pytest.raises(request_tail.RequiredTailError) as failed:
        await mcp._dispatch_batch([request(1), request(2)], dispatch)
    assert "private insert detail" not in str(failed.value)
    assert visited == [1] and attempts == ["required"] and ordinary == []
    assert bag.tail.required_failed and not request_tail.required_active()
    with pytest.raises(request_tail.RequiredTailError):
        await request_tail.settle(bag.tail)
    assert attempts == ["required"] and ordinary == ["ordinary"]


async def test_ordinary_entries_keep_deferred_work_until_final_settlement(bag):
    ordinary = []

    async def cleanup():
        ordinary.append("ordinary")

    async def dispatch(entry):
        assert ordinary == [] and not request_work.mirror_marked()
        if entry["id"] == 1:
            assert request_tail.defer(cleanup)
        return frame(entry["id"], {"ordinary": entry["id"]})

    expected = [frame(index, {"ordinary": index}) for index in (1, 2)]
    assert await mcp._dispatch_batch([request(1), request(2)], dispatch) == expected
    assert ordinary == []
    await request_tail.settle(bag.tail)
    assert ordinary == ["ordinary"]


async def test_cancelled_entry_drain_joins_its_commit_before_propagating(bag):
    entered, release = asyncio.Event(), asyncio.Event()
    committed, later_entries = [], []

    async def commit():
        entered.set()
        await release.wait()
        committed.append("required")

    async def dispatch(entry):
        if entry["id"] == 1:
            assert request_work.mark_mirrored()
            assert request_tail.defer_required(commit)
        else:
            later_entries.append(entry["id"])
        return frame(entry["id"], {"content": "CHECKED-CONTENT"})

    pending = asyncio.create_task(mcp._dispatch_batch([request(1), request(2)], dispatch))
    try:
        await asyncio.sleep(0)
        assert not pending.done(), "the entry must wait for its required commit"
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert committed == ["required"] and later_entries == []
        assert request_work.http_mirror_marked()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await request_tail.settle(bag.tail)


@pytest.mark.parametrize("shape", ["one", "multi", "notification"])
async def test_only_a_retained_single_frame_renews_its_publication_dependencies(bag, shape):
    checks, commits = [], []

    async def check():
        checks.append("private")

    async def commit():
        commits.append("event")

    async def dispatch(entry):
        if entry.get("id") == 2:
            return frame(2, {"ordinary": True})
        assert request_work.mark_mirrored()
        assert request_tail.defer_required(commit)
        request_tail.retain_publication(object(), check, lambda: True)
        return None if "id" not in entry else frame(1, {"private": True})

    entries = [request(1)]
    if shape == "multi":
        entries.append(request(2))
    elif shape == "notification":
        entries[0].pop("id")
    result = await mcp._dispatch_batch(entries, dispatch)
    assert commits == ["event"]
    await request_tail.settle(bag.tail)
    await request_tail._publication_barrier(bag.tail)
    assert checks == (["private"] if shape == "one" else [])
    assert ("private" in json.dumps(result)) is (shape == "one")


@pytest.mark.parametrize("notification", [False, True])
async def test_single_endpoint_keeps_response_proofs_and_discards_notification_proofs(
    monkeypatch, notification
):
    from fastapi import Request, Response

    routers = []
    mcp.attach(SimpleNamespace(include_router=routers.append))
    endpoint = next(route.endpoint for route in routers[0].routes if route.path == "/mcp")
    live, captured, sent = [True], [], []

    async def check():
        if not live[0]:
            raise ValueError("private-access-changed")

    async def commit():
        captured.append("event")
        live[0] = False

    async def dispatch(req, *_args, **_kwargs):
        request_work.begin()
        assert request_work.mark_mirrored()
        request_tail.retain_publication(object(), check, lambda: live[0])
        assert request_tail.defer_required(commit)
        return None if "id" not in req else frame(1, {"private": True})

    monkeypatch.setattr(mcp, "_effective_auth_mode", lambda: "open")
    monkeypatch.setattr(mcp, "_verify_bearer", lambda _request: None)
    monkeypatch.setattr(mcp, "_handle_request", dispatch)
    payload = request(1, "ping")
    if notification:
        payload.pop("id")

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode()}

    async def app(scope, _receive, send):
        await endpoint(Request(scope, receive), Response())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"constant"})

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [], "query_string": b""}
    if notification:
        await request_tail.RequestTailMiddleware(app)(scope, None, send)
        assert len(sent) == 2
    else:
        with pytest.raises(request_tail.RequiredTailError):
            await request_tail.RequestTailMiddleware(app)(scope, None, send)
        assert sent == []
    assert captured == ["event"]
