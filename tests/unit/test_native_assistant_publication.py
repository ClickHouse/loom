"""Whole native answers retain nested tool proofs until their last response send."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse

from server.assistant.app import routes, runtime, tools
from server.core import request_tail
from server.operations.base import Role
from server.settings import settings


@pytest.fixture
def publication(monkeypatch):
    from server.transport import mcp, mcp_call
    from tests.unit.mcp_probe import _Out, serve_probe

    probe = serve_probe(monkeypatch)
    principal = replace(
        probe["tokens"]["alice"],
        is_service_account=False,
        consumer="loom-assistant",
        token_id="assistant-answer-publication",
    )
    monkeypatch.setattr(
        "server.auth.jwt.authenticate_token", AsyncMock(side_effect=lambda token: principal)
    )
    state = SimpleNamespace(current=True, checks=0, owner=object(), stage="", events=[])

    async def renew():
        state.checks += 1
        state.events.append("proof")
        if not state.current:
            raise PermissionError("source access changed")

    async def handler(inp):
        request_tail.retain_publication(state.owner, renew, lambda: state.current)
        return _Out()

    operation = replace(
        mcp_call.TOOLS_BY_NAME["loom_test_refusal_probe"],
        name="knowledge.get",
        handler=handler,
        idempotent=True,
        required_role=Role.VIEWER,
    )
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, "loom_knowledge_get", operation)

    async def readable(auth, org, namespace):
        return True, False

    monkeypatch.setattr("server.transport.mcp_auth._may_read", readable)
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    app = FastAPI()
    mcp.attach(app)
    app.add_middleware(request_tail.RequestTailMiddleware)
    monkeypatch.setattr(tools, "_app", app)

    async def authorize(*args):
        return None

    ticket = SimpleNamespace(mirrored=True, release=lambda: None)

    async def admitted(*args):
        return ticket

    async def stream(ticket, messages, answer, **kwargs):
        await tools.Tools("acme", "n", "alice", "answer")._request(
            "loom_knowledge_get", {"org": "acme", "namespace": "n"}
        )
        if state.stage == "model":
            state.current = False
        answer.text.append("private answer")
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "authorize", authorize)
    monkeypatch.setattr(runtime, "open_answer", admitted)
    monkeypatch.setattr(runtime, "stream", stream)
    state.auth = probe["tokens"]["alice"]
    state.ticket = ticket
    return state


async def _whole_response(state, surface):
    async def accounting():
        state.events.append("accounting")
        if state.stage == "accounting":
            state.current = False

    def close(error):
        assert error is None
        request_tail.defer_required(accounting)

    messages = [{"role": "user", "content": "our policy"}]
    if surface == "chat":
        answer = runtime.Answer(mirrored=True)
        held = SimpleNamespace(answer=answer, close=close, row=SimpleNamespace(request_id="r"))
        return await routes._whole(runtime.stream(state.ticket, messages, answer), held)
    answer = await runtime.ask(state.auth, "acme", "n", messages)
    close(None)
    return JSONResponse({"answer": "".join(answer.text)})


@pytest.mark.parametrize("surface", ["chat", "operation"])
@pytest.mark.parametrize("stage", ["", "model", "accounting", "headers"])
def test_whole_answer_renews_tool_proof_after_accounting_and_headers(publication, surface, stage):
    state = publication
    state.stage = stage
    sent = []

    async def app(scope, receive, send):
        response = await _whole_response(state, surface)
        await response(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.start" and stage == "headers":
            state.current = False

    async def scenario():
        call = request_tail.RequestTailMiddleware(app)
        scope = {"type": "http", "method": "POST", "path": "/answer", "headers": []}
        if stage:
            with pytest.raises(request_tail.RequiredTailError):
                await call(scope, receive, send)
        else:
            await call(scope, receive, send)

    asyncio.run(scenario())
    body = b"".join(m.get("body", b"") for m in sent)
    if stage:
        assert b"private answer" not in body
    else:
        payload = json.loads(body)
        text = (
            payload["choices"][0]["message"]["content"] if surface == "chat" else payload["answer"]
        )
        assert text == "private answer"
        assert state.events.index("accounting") < len(state.events) - 1
        assert state.events[-1] == "proof" and state.checks >= 2


@pytest.mark.parametrize("whole_page", [False, True])
def test_help_receipts_preserve_the_verified_section_anchor(whole_page):
    from server.assistant.app import evidence
    from server.assistant.app.tools import Receipt

    section = {"section": "Sharing > Collections", "anchor": "collections"}
    result = {
        "found": True,
        "path": "features/sharing.md",
        "text": "# Sharing\n\n## Collections\n\nReturned section text.",
        "truncated": False,
        **section,
    }
    if whole_page:
        result.update(section="", anchor="", sections=[section])
    receipt = Receipt("help.get", {}, result, "model_tool", 1)
    (citation,) = evidence.verify("[help:features/sharing.md § Sharing > Collections]", [receipt])
    assert citation.verified and citation.ref == "features/sharing.md"
    assert citation.anchor == "collections"


@pytest.mark.parametrize("mirrored", [False, True])
@pytest.mark.parametrize("stage", ["", "model", "accounting"])
def test_stream_terminal_renews_nested_proofs_after_accounting(
    publication, monkeypatch, mirrored, stage
):
    state = publication
    state.stage = stage
    state.ticket.mirrored = mirrored
    sent = []
    request = tools.Tools._request

    async def child_request(self, *args):
        return await asyncio.create_task(request(self, *args))

    monkeypatch.setattr(tools.Tools, "_request", child_request)

    async def accounting():
        state.events.append("accounting")
        if stage == "accounting":
            state.current = False

    def close(error):
        if error is None:
            request_tail.defer_required(accounting)

    held = SimpleNamespace(
        ticket=state.ticket,
        answer=runtime.Answer(mirrored=mirrored),
        close=close,
        finished=False,
        row=SimpleNamespace(request_id="stream-publication"),
    )

    async def app(scope, receive, send):
        chunks = runtime.stream(
            state.ticket, [{"role": "user", "content": "our policy"}], held.answer
        )
        response = routes._AnswerStream(routes._streamed(chunks, held), held)
        await response(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def scenario():
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/answer",
            "headers": [],
            "asgi": {"spec_version": "2.4"},
        }
        call = request_tail.RequestTailMiddleware(app)
        if stage:
            with pytest.raises(request_tail.RequiredTailError):
                await call(scope, receive, send)
        else:
            await call(scope, receive, send)

    asyncio.run(scenario())
    body = b"".join(message.get("body", b"") for message in sent)
    assert (b"[DONE]" in body) is (not stage)
    assert state.events[-1] == "proof"
    assert state.events.index("accounting") < len(state.events) - 1


@pytest.mark.parametrize("mirrored", [False, True])
@pytest.mark.parametrize("loss", ["", "model", "send"])
def test_stream_never_sends_private_content_after_source_proof_loss(publication, mirrored, loss):
    state = publication
    state.ticket.mirrored = mirrored
    sent, closed, accounted, released = [], [], [], []

    async def chunks():
        try:
            await asyncio.create_task(
                tools.Tools("acme", "n", "alice", "answer")._request(
                    "loom_knowledge_get", {"org": "acme", "namespace": "n"}
                )
            )
            yield b"data: notice\n\n"
            if loss == "model":
                state.current = False
            await asyncio.sleep(0)
            yield b'data: {"choices":[{"delta":{"content":"private-canary"}}]}\n\n'
            yield b"data: [DONE]\n\n"
        finally:
            closed.append(True)

    async def accounting():
        accounted.append(True)

    def close(error):
        if not released:
            released.append(error)
            assert request_tail.defer_required(accounting)

    held = SimpleNamespace(
        ticket=state.ticket,
        close=close,
        finished=False,
        row=SimpleNamespace(request_id="stream-source-proof"),
    )

    async def app(scope, receive, send):
        await routes._AnswerStream(routes._streamed(chunks(), held), held)(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)
        if loss == "send" and message.get("body") == b"data: notice\n\n":
            state.current = False

    async def scenario():
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/answer",
            "headers": [],
            "asgi": {"spec_version": "2.4"},
        }
        call = request_tail.RequestTailMiddleware(app)
        if loss:
            with pytest.raises(request_tail.RequiredTailError):
                await call(scope, receive, send)
        else:
            await call(scope, receive, send)

    asyncio.run(scenario())
    body = b"".join(message.get("body", b"") for message in sent)
    assert (b"private-canary" in body) is (not loss)
    assert (b"[DONE]" in body) is (not loss)
    assert closed == accounted == [True]
    assert len(released) == 1


def test_stream_can_close_from_a_different_task_after_a_provisional_frame():
    closed = []

    async def source():
        try:
            yield b"data: provisional\n\n"
        finally:
            closed.append(True)

    async def scenario():
        async with request_tail.streaming_scope():
            stream = routes._streamed(source(), SimpleNamespace(finished=False))
            assert await asyncio.create_task(anext(stream)) == b"data: provisional\n\n"
            await asyncio.create_task(stream.aclose())

    asyncio.run(scenario())
    assert closed == [True]
