"""Selected private answers renew after required writes and awaited headers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth.context import current_auth_var
from server.core import request_tail, request_work
from server.core.diagnostics import private_work
from server.knowledge.api.models_knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)
from server.knowledge.app import knowledge_search, source_downloads
from server.knowledge.app.source_corpus import LostCorpus
from server.observability import events


@pytest.mark.parametrize("loss_at", ["write", "headers", "never"])
async def test_search_publication_never_outlives_selected_access(monkeypatch, loss_at):
    access = SimpleNamespace(live=True, checks=0, writes=0)
    caller = object()
    sent = []

    async def check():
        assert current_auth_var.get() is caller
        access.checks += 1
        if not access.live:
            raise LostCorpus()

    proofs = SimpleNamespace(check=check, fresh=lambda: access.live, mirrored=True)
    output = KnowledgeSearchOutput(
        embedding_model="fake",
        results=[
            _SearchHit(
                source_uri="drive:private",
                chunk_idx=0,
                content="private-canary",
                tags=[],
                distance=0,
                similarity=1,
            )
        ],
    )
    checked = knowledge_search.CheckedSearch(output, proofs, SimpleNamespace(marked=True), [], 0)
    monkeypatch.setattr(
        knowledge_search, "checked_knowledge_search", AsyncMock(return_value=checked)
    )

    async def insert(table, rows, *, settings, required_columns):
        assert table == "loom.agent_events_private" and required_columns == frozenset(
            {"mirror_marked"}
        )
        await asyncio.sleep(0)
        access.writes += 1
        if loss_at == "write":
            access.live = False

    monkeypatch.setattr(events, "get_client", lambda: SimpleNamespace(insert=insert))
    monkeypatch.setattr(events, "stamp_row", lambda row: row)
    monkeypatch.setattr(events, "for_this_cell", lambda event: event)
    event = events.AgentEvent(
        ts=datetime.fromtimestamp(0, UTC),
        org="org",
        namespace="team",
        agent="reader",
        user_id="user",
        session_id="session",
        operation="knowledge.search",
        request_id="request",
        latency_ms=1,
        success=True,
        kind="request",
        mirror_marked=True,
    )

    async def write():
        with private_work():
            await events.write_marked_request_event(event)

    async def app(scope, receive, send):
        token = request_work._current.set(None)
        auth = current_auth_var.set(caller)
        try:
            request_work.begin()
            assert request_work.mark_mirrored()
            result = await knowledge_search.handle_knowledge_search(
                KnowledgeSearchInput(org="org", namespace="team", query="document")
            )
            assert request_tail.defer_required(write)
        finally:
            current_auth_var.reset(auth)
            request_work._current.reset(token)
        # Endpoint/dependency auth has unwound before the response is serialized.
        assert current_auth_var.get() is not caller
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": result.model_dump_json().encode()})

    async def send(message):
        sent.append(message)
        await asyncio.sleep(0)
        if message["type"] == "http.response.start" and loss_at == "headers":
            access.live = False

    if loss_at == "never":
        await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
        assert b"private-canary" in sent[-1]["body"]
    else:
        with pytest.raises(request_tail.RequiredTailError):
            await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
        assert not any(b"private-canary" in message.get("body", b"") for message in sent)
        assert len(sent) == (1 if loss_at == "headers" else 0)
    assert access.writes == 1


async def test_later_renewal_cannot_publish_after_an_earlier_proof_expires():
    live = [True]
    sent = []

    async def first():
        assert live[0]

    async def second():
        await asyncio.sleep(0)
        live[0] = False

    async def app(scope, receive, send):
        request_tail.retain_publication(object(), first, lambda: live[0])
        request_tail.retain_publication(object(), second, lambda: True)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    with pytest.raises(request_tail.RequiredTailError):
        await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, sent.append)
    assert sent == []


@pytest.mark.parametrize("commit", [False, True])
async def test_abandoned_nested_branch_does_not_keep_its_private_dependencies(commit):
    checks = []

    async def check():
        checks.append("selected")

    async def discarded():
        raise LostCorpus()

    async def app(scope, receive, send):
        with request_tail.publication_group() as outer:
            request_tail.retain_publication(object(), check, lambda: True)
            with request_tail.publication_group():
                request_tail.retain_publication(object(), discarded, lambda: False)
            if commit:
                outer.commit()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(_message):
        pass

    await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
    assert checks == (["selected"] if commit else [])


@pytest.mark.parametrize("failure", ["self_cancel", "timeout"])
async def test_proof_failure_is_a_constant_refusal_and_joins_its_task(monkeypatch, failure):
    ended = asyncio.Event()

    async def check():
        try:
            if failure == "self_cancel":
                raise asyncio.CancelledError()
            await asyncio.Future()
        finally:
            ended.set()

    async def app(scope, receive, send):
        request_tail.retain_publication(object(), check, lambda: True)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    monkeypatch.setattr(request_tail, "REQUIRED_STEP_TIMEOUT_S", 0.01)
    with pytest.raises(
        request_tail.RequiredTailError, match="Required request work was not confirmed"
    ):
        await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, None)
    assert ended.is_set()


async def test_caller_cancellation_during_renewal_propagates_and_joins_proof():
    entered, ended = asyncio.Event(), asyncio.Event()

    async def check():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            ended.set()

    async def app(scope, receive, send):
        request_tail.retain_publication(object(), check, lambda: True)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    pending = asyncio.create_task(
        request_tail.RequestTailMiddleware(app)({"type": "http"}, None, None)
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert ended.is_set()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_raw_download_marks_have_one_required_event_on_every_exit(monkeypatch, outcome):
    captured, sent = [], []

    async def write(event):
        await asyncio.sleep(0)
        captured.append(event)

    monkeypatch.setattr(events, "write_marked_request_event", write)

    async def app(scope, receive, send):
        auth = current_auth_var.set(None)
        try:
            try:
                with source_downloads.download_request("org", "team"):
                    assert request_work.mark_mirrored()
                    if outcome == "failure":
                        raise ValueError("private-backend-detail")
                    if outcome == "cancel":
                        raise asyncio.CancelledError()
            except ValueError:
                pass
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"constant"})
        finally:
            current_auth_var.reset(auth)

    async def send(message):
        assert len(captured) == 1
        sent.append(message)

    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
        assert not sent
    else:
        await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
    assert len(captured) == 1
    event = captured[0]
    assert (event.operation, event.org, event.namespace, event.transport) == (
        "knowledge.download",
        "org",
        "team",
        "http",
    )
    assert event.mirror_marked and event.success is (outcome == "success")
    assert event.error_code == ("" if outcome == "success" else "REQUEST_INTERRUPTED")
    assert "private-backend-detail" not in repr(event.args)


@pytest.mark.parametrize("later_chunk", [b"", b"still-private"])
async def test_stream_completion_after_delivered_payload_keeps_next_request_usable(later_chunk):
    from starlette.responses import Response, StreamingResponse

    live = True
    checks = []
    sent = []

    async def check():
        checks.append(live)
        if not live:
            raise LostCorpus()

    async def chunks():
        yield b"private-payload"
        if later_chunk:
            yield later_chunk

    async def app(scope, receive, send):
        if scope["path"] == "/private":
            request_tail.retain_publication(object(), check, lambda: live)
            response = StreamingResponse(
                chunks(),
                headers={"content-length": str(len(b"private-payload") + len(later_chunk))},
            )
        else:
            response = Response(b"ordinary")
        await response(scope, receive, send)

    async def send(message):
        nonlocal live
        sent.append(message)
        # A Content-Length client can start its next request after these bytes.
        if message.get("body") == b"private-payload":
            live = False
        await asyncio.sleep(0)

    middleware = request_tail.RequestTailMiddleware(app)
    scope = {"type": "http", "path": "/private", "asgi": {"spec_version": "2.4"}}
    if later_chunk:
        with pytest.raises(request_tail.RequiredTailError):
            await middleware(scope, None, send)
        assert not any(message.get("body") == later_chunk for message in sent)
        assert checks == [True, True, False]
    else:
        await middleware(scope, None, send)
        assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
        assert checks == [True, True]
        await middleware({**scope, "path": "/ordinary"}, None, send)
        assert sent[-1]["body"] == b"ordinary"
