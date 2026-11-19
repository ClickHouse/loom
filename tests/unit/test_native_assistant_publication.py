"""Whole native answers retain nested tool proofs until their last response send."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

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
    monkeypatch.setattr("server.auth.jwt.verify_token", lambda token: principal)
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
    result = {"found": True, "path": "features/sharing.md", **section}
    if whole_page:
        result.update(section="", anchor="", sections=[section])
    receipt = Receipt("help.get", {}, result, "model_tool", 1)
    (citation,) = evidence.verify("[help:features/sharing.md § Sharing > Collections]", [receipt])
    assert citation.verified and citation.ref == "features/sharing.md"
    assert citation.anchor == "collections"
