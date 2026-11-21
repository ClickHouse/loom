"""Native provider wire fixtures and completion publication boundaries, without networks."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from server.assistant.app import routes, tools
from server.core import request_tail
from server.core.errors import AssistantUpstreamError
from server.llm import agent_http
from server.llm.agent import Turn
from server.llm.customer import Customer
from server.operations.base import Role
from server.settings import settings


@pytest.mark.parametrize("provider", ["openai", "anthropic", "local"])
def test_provider_request_uses_explicit_streaming_tools_without_temperature(monkeypatch, provider):
    seen = []
    if provider == "anthropic":
        frames = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 100}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "answer"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 10},
            },
            {"type": "message_stop"},
        ]
    else:
        frames = [
            {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        ]
    wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    if provider != "anthropic":
        wire += "data: [DONE]\n\n"

    def reply(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=wire)

    async def customer():
        return Customer()

    async def bearer(key, url):
        return "fixture-key"

    monkeypatch.setattr(agent_http.customer, "require", customer)
    monkeypatch.setattr(agent_http.gateway, "bearer", bearer)
    monkeypatch.setattr(agent_http, "provider_key", lambda _: "fixture-key")
    for field in ("openai_base_url", "anthropic_base_url", "local_llm_base_url"):
        monkeypatch.setattr(settings, field, "https://provider.invalid/v1")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            monkeypatch.setattr(agent_http, "get_model_client", lambda: client)
            turn = Turn()
            text = [
                part
                async for part in agent_http.HTTPAgentModel(provider, "explicit-model").stream(
                    [
                        {"role": "system", "content": "rules"},
                        {"role": "user", "content": "question"},
                    ],
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": "loom_memory_get",
                                "description": "read",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    100,
                    turn,
                )
            ]
            assert text == ["answer"] and turn.stop == "stop" and turn.usage_complete

    asyncio.run(scenario())
    assert len(seen) == 1 and seen[0]["model"] == "explicit-model" and seen[0]["stream"]
    assert seen[0]["tools"] and "temperature" not in seen[0]
    assert "reasoning_effort" not in seen[0]


@pytest.mark.parametrize(
    "data",
    [b"data: null\n\n", b"data: [broken\n\n", b"data: {}", b'data: {"error": "refused"}\n\n'],
)
def test_malformed_sse_frames_refuse(data):
    from server.llm.agent import sse

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))
        ) as client:
            async with client.stream("GET", "https://provider.invalid") as response:
                with pytest.raises(AssistantUpstreamError):
                    [frame async for frame in sse(response)]

    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_required_cost_delivery_precedes_done(monkeypatch, fail):
    order = []

    async def source():
        try:
            yield b'data: {"choices":[]}\n\n'
            yield b"data: [DONE]\n\n"
        finally:
            order.append("producer_closed")

    async def settle():
        order.append("delivery")
        if fail:
            raise request_tail.RequiredTailError()

    monkeypatch.setattr(request_tail, "publish_current", settle)
    held = SimpleNamespace(close=lambda error: order.append("ledger"), finished=False)

    async def scenario():
        received = []
        try:
            async for frame in routes._streamed(source(), held):
                received.append(frame)
                if frame == b"data: [DONE]\n\n":
                    assert order == ["producer_closed", "ledger", "delivery"]
        except request_tail.RequiredTailError:
            assert fail
        assert (b"data: [DONE]\n\n" in received) is not fail

    asyncio.run(scenario())


def test_native_tool_uses_authenticated_mcp_and_revocation(monkeypatch):
    from dataclasses import replace

    from fastapi import FastAPI

    from server.auth.jwt import TokenError
    from server.transport import mcp, mcp_call
    from tests.unit.mcp_probe import _Out, serve_probe

    probe = serve_probe(monkeypatch)
    principal = replace(
        probe["tokens"]["alice"], consumer="loom-assistant", token_id="assistant-answer-native"
    )
    seen = []

    async def handler(inp):
        from server.auth.context import current_auth
        from server.observability.run_context import current_ledger_run

        assert current_ledger_run() == "answer-owned"
        assert current_auth().subject == principal.subject
        seen.append((inp.org, inp.namespace))
        return _Out()

    op = replace(
        mcp_call.TOOLS_BY_NAME["loom_test_refusal_probe"],
        name="memory.get",
        handler=handler,
        idempotent=True,
        required_role=Role.VIEWER,
    )
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, "loom_memory_get", op)
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    app = FastAPI()
    mcp.attach(app)
    monkeypatch.setattr(tools, "_app", app)
    revoked = False

    def verify(token):
        assert token == "alice"
        if revoked:
            raise TokenError("revoked")
        return principal

    monkeypatch.setattr("server.auth.jwt.authenticate_token", AsyncMock(side_effect=verify))

    async def readable(auth, org, ns):
        assert auth.subject == principal.subject
        return (org, ns) == ("acme", "n"), False

    monkeypatch.setattr("server.transport.mcp_auth._may_read", readable)

    async def scenario():
        nonlocal revoked
        native = tools.Tools("acme", "n", "alice", "answer-owned")
        result = await native.call("loom_memory_get", {"memory_key": "one"}, origin="host_context")
        assert result.result == {"ok": True} and seen == [("acme", "n")]
        revoked = True
        with pytest.raises(AssistantUpstreamError):
            await native.call("loom_memory_get", {"memory_key": "two"}, origin="model_tool")
        assert len(seen) == 1

    asyncio.run(scenario())


def test_bedrock_binary_stream_uses_guarded_http_and_closes(monkeypatch):
    import struct
    import zlib

    from server.llm import agent_bedrock

    def event(kind, data):
        def header(name, value):
            return (
                bytes([len(name)])
                + name.encode()
                + b"\x07"
                + struct.pack(">H", len(value))
                + value.encode()
            )

        headers = header(":message-type", "event") + header(":event-type", kind)
        payload = json.dumps(data).encode()
        prelude = struct.pack(">II", len(headers) + len(payload) + 16, len(headers))
        body = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + payload
        return body + struct.pack(">I", zlib.crc32(body))

    wire = b"".join(
        event(kind, frame)
        for kind, frame in [
            ("messageStart", {"role": "assistant"}),
            ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "answer"}}),
            ("contentBlockStop", {"contentBlockIndex": 0}),
            ("messageStop", {"stopReason": "end_turn"}),
            ("metadata", {"usage": {"inputTokens": 100, "outputTokens": 10}}),
        ]
    )
    sent = []
    checked = []

    def reply(request):
        sent.append(request)
        return httpx.Response(200, content=wire)

    async def customer():
        return Customer()

    monkeypatch.setattr(agent_bedrock.customer, "require", customer)

    async def signed(payload, headers):
        assert payload["modelId"] == "explicit.profile"
        assert headers["Accept"] == "application/vnd.amazon.eventstream"
        return (
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/explicit.profile/converse-stream",
            json.dumps({k: v for k, v in payload.items() if k != "modelId"}).encode(),
            {"Authorization": "fixture"},
        )

    monkeypatch.setattr(agent_bedrock, "_signed_request", signed)
    monkeypatch.setattr(agent_bedrock, "refuse_off_gateway", lambda url: checked.append(url))
    monkeypatch.setattr(settings, "bedrock_region", "us-east-1")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            monkeypatch.setattr(agent_bedrock, "get_model_client", lambda: client)
            turn = Turn()
            text = [
                part
                async for part in agent_bedrock.BedrockAgentModel(
                    "bedrock:explicit.profile"
                ).stream(
                    [{"role": "system", "content": "rules"}, {"role": "user", "content": "q"}],
                    [],
                    100,
                    turn,
                )
            ]
            assert text == ["answer"] and turn.usage_complete

    asyncio.run(scenario())
    assert len(sent) == 1 and len(checked) == 1
    assert str(sent[0].url).endswith("/model/explicit.profile/converse-stream")
    assert json.loads(sent[0].content)["inferenceConfig"] == {"maxTokens": 100}


def test_bedrock_cancel_joins_pending_signer_without_sending_a_request(monkeypatch):
    from server.llm import agent_bedrock

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def sign(operation, payload, headers, region):
        assert operation == "converse_stream"
        started.set()
        try:
            assert release.wait(5), "credential fixture was not released"
            return "https://provider.invalid/", b"{}", {}
        finally:
            finished.set()

    monkeypatch.setattr(agent_bedrock.bedrock, "_signed", sign)

    async def scenario():
        task = asyncio.create_task(agent_bedrock._signed_request({"modelId": "fixture"}, {}))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()

    asyncio.run(scenario())


def test_unknown_usage_trace_does_not_invent_zero_tokens():
    from server.observability import genai

    class Span:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, key, value):
            self.attrs[key] = value

    span = Span()
    generation = genai._Generation(span, "claude-haiku-4-5")
    generation.note_attempt()
    generation.record_accounting(
        usage_status="unknown", price_model="claude-haiku-4-5", estimated_cost=0.01
    )
    assert span.attrs["loom.llm.usage_status"] == "unknown"
    assert span.attrs["loom.llm.cost_estimated"]
    assert "gen_ai.usage.input_tokens" not in span.attrs
    assert "gen_ai.usage.cost" not in span.attrs


def test_provider_response_cleanup_joins_repeated_cancellation():
    from server.llm.agent import model_stream

    async def scenario():
        closing, release = asyncio.Event(), asyncio.Event()
        closed = []

        class Context:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                closing.set()
                await release.wait()
                closed.append(True)

        class Client:
            def stream(self):
                return Context()

        async def consume():
            async with model_stream(Client()):
                await asyncio.Event().wait()

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        task.cancel()
        await closing.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]

    asyncio.run(scenario())


def test_native_host_calls_loom_before_the_first_model_turn(monkeypatch):
    from dataclasses import replace

    from fastapi import FastAPI

    from server.assistant.app import agent, runtime
    from server.llm import agent as models
    from server.observability.run_context import current_ledger_run
    from server.transport import mcp, mcp_call
    from tests.unit.mcp_probe import _Out, serve_probe

    probe = serve_probe(monkeypatch)
    principal = replace(
        probe["tokens"]["alice"],
        is_service_account=False,
        consumer="loom-assistant",
        token_id="assistant-answer-composed",
    )
    state = agent.State()
    invoked = []
    for name in ("memory.search", "knowledge.search"):

        async def handler(inp, operation=name):
            assert current_ledger_run() == state.run_id
            assert (inp.org, inp.namespace) == ("acme", "n")
            invoked.append(operation)
            return _Out()

        operation = replace(
            mcp_call.TOOLS_BY_NAME["loom_test_refusal_probe"],
            name=name,
            handler=handler,
            idempotent=True,
            required_role=Role.VIEWER,
        )
        monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, "loom_" + name.replace(".", "_"), operation)

    async def readable(auth, org, ns):
        assert auth.subject == principal.subject
        return (org, ns) == ("acme", "n"), False

    async def affordable(*args):
        return None

    monkeypatch.setattr(runtime, "_afford", affordable)
    monkeypatch.setattr(
        "server.auth.jwt.authenticate_token", AsyncMock(side_effect=lambda token: principal)
    )
    monkeypatch.setattr("server.transport.mcp_auth._may_read", readable)
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "assistant_provider", "fake")
    monkeypatch.setattr(settings, "assistant_model", "")
    monkeypatch.setattr(settings, "assistant_price_model", "")
    app = FastAPI()
    mcp.attach(app)
    monkeypatch.setattr(tools, "_app", app)

    class Replay:
        async def stream(self, messages, descriptors, maximum, turn):
            assert invoked == ["memory.search", "knowledge.search"]
            assert len([m for m in messages if m["role"] == "tool"]) == 2
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = models.Usage(100, 10), "stop"
            turn.text = "Nothing stored in Loom answers this."
            yield turn.text

    monkeypatch.setattr(models, "model_for", lambda *_: Replay())

    async def scenario():
        return [
            frame
            async for frame in agent.run(
                "acme",
                "n",
                "alice",
                "viewer",
                [{"role": "user", "content": "our deployment policy"}],
                state,
            )
        ]

    frames = asyncio.run(scenario())
    assert state.turns == 1 and all(r.origin == "host_context" for r in state.receipts)
    assert frames[-1]["usage"]["prompt_tokens"] == 100


@pytest.mark.parametrize("origin", ["host_context", "model_tool"])
@pytest.mark.parametrize("affordable", [False, True])
def test_tool_cost_ceiling_survives_authenticated_mcp_context(
    monkeypatch: pytest.MonkeyPatch, origin: str, affordable: bool
) -> None:
    from dataclasses import replace

    from fastapi import FastAPI

    from server.assistant.app import agent, runtime
    from server.auth.context import current_auth
    from server.clients import _http
    from server.observability import cost_accumulator
    from server.observability.run_context import current_ledger_run
    from server.transport import mcp, mcp_call
    from tests.unit.mcp_probe import _Out, serve_probe

    probe = serve_probe(monkeypatch)
    principal = replace(
        probe["tokens"]["alice"],
        is_service_account=False,
        consumer="loom-assistant",
        token_id="assistant-tool-budget",
    )
    initial_spent = 0.0005
    state = agent.State(spent=initial_spent)
    sent = []
    children = []
    entered = []

    async def dispatch(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    async def handler(inp):
        assert current_auth().subject == principal.subject
        assert current_ledger_run() == state.run_id
        assert (inp.org, inp.namespace) == ("acme", "n")
        entered.append(inp.query)
        children.append(cost_accumulator.current())
        async with _http._build(
            transport=httpx.MockTransport(dispatch), event_hooks={"request": [_http._on_request]}
        ) as client:
            await client.post(
                "https://provider.invalid/embeddings",
                json={"model": "text-embedding-3-small", "input": "x" * 1000},
            )
        cost_accumulator.report_embed("text-embedding-3-small", 1000)
        return _Out()

    operation = replace(
        mcp_call.TOOLS_BY_NAME["loom_test_refusal_probe"],
        name="memory.search",
        handler=handler,
        idempotent=True,
        required_role=Role.VIEWER,
    )
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, "loom_memory_search", operation)

    async def readable(auth, org, namespace):
        assert auth.subject == principal.subject
        return (org, namespace) == ("acme", "n"), False

    async def share_available(*args):
        return None

    monkeypatch.setattr(runtime, "_afford", share_available)
    monkeypatch.setattr("server.auth.jwt.authenticate_token", AsyncMock(return_value=principal))
    monkeypatch.setattr("server.transport.mcp_auth._may_read", readable)
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "inference_gateway_hosts", "provider.invalid")
    monkeypatch.setattr(settings, "inference_customer_header", "")
    monkeypatch.setattr(
        settings,
        "assistant_answer_ceiling_usd",
        initial_spent + (1 if affordable else 0.000001),
    )
    app = FastAPI()
    mcp.attach(app)
    monkeypatch.setattr(tools, "_app", app)

    async def scenario():
        parent = cost_accumulator.begin_op()
        cost_accumulator.report_embed("text-embedding-3-small", 7)
        previous_cost = parent.cost_usd
        native = tools.Tools("acme", "n", "alice", state.run_id)
        if affordable:
            receipt = await agent._call(
                native, "loom_memory_search", {"query": "policy"}, origin=origin, state=state
            )
            assert receipt.result == {"ok": True} and receipt.origin == origin
            assert len(sent) == 1 and native.receipts == [receipt]
            charge = cost_accumulator.embed_cost_usd("text-embedding-3-small", 1000)
            assert receipt.cost_usd == children[0].cost_usd == charge
            assert state.spent >= initial_spent + charge
        else:
            with pytest.raises(AssistantUpstreamError):
                await agent._call(
                    native, "loom_memory_search", {"query": "policy"}, origin=origin, state=state
                )
            assert sent == [] and native.receipts == []
            assert state.spent == initial_spent and children[0].cost_usd == 0
        assert entered == ["policy"] and children[0] is not parent
        assert cost_accumulator.current() is parent and parent.cost_usd == previous_cost

    asyncio.run(scenario())


@pytest.fixture
def native_row_reader(monkeypatch):
    from dataclasses import replace

    from fastapi import FastAPI

    from server.auth import authz, delegation
    from server.auth.jwt import TokenError
    from server.code.app import member_writes
    from server.memory.api.models_crud import MemoryGetInput, MemoryGetOutput
    from server.transport import mcp, mcp_call
    from tests.unit.mcp_probe import serve_probe

    authorize_op = member_writes.authorize_op
    probe = serve_probe(monkeypatch)
    monkeypatch.setattr(member_writes, "authorize_op", authorize_op)
    principal = replace(
        probe["tokens"]["alice"],
        consumer="loom-assistant",
        token_id="assistant-row-read",
        creator="parent-user",
        roles=(Role.ADMIN,),
        ns_roles=(("n", "viewer"),),
    )
    state = SimpleNamespace(principal=principal, grant=Role.ADMIN, revoked=False, seen=[])

    async def creator_grant(org, namespace, creator):
        assert (org, creator) == ("acme", "parent-user")
        return state.grant

    async def handler(inp):
        from server.auth.context import current_auth

        assert isinstance(inp, MemoryGetInput)
        assert current_auth() == state.principal
        state.seen.append(inp)
        found = (inp.user_id, inp.session_id) in {
            ("person-a", "-"),
            ("-", "conversation-a"),
            ("person-a", "conversation-a"),
        }
        return MemoryGetOutput(found=found, value="selected row" if found else None)

    def verify(token):
        assert token == "alice"
        if state.revoked:
            raise TokenError("revoked")
        return state.principal

    monkeypatch.setattr(authz, "_creator_grant", creator_grant)
    monkeypatch.setattr(authz, "_check_provisioning", AsyncMock())
    monkeypatch.setattr(authz, "may_be_restricted", AsyncMock(return_value=False))
    monkeypatch.setattr(authz, "is_org_readable_code_namespace", AsyncMock(return_value=False))
    monkeypatch.setattr(delegation, "creator_account_active", AsyncMock(return_value=True))
    monkeypatch.setattr("server.auth.jwt.authenticate_token", AsyncMock(side_effect=verify))
    original = mcp_call.TOOLS_BY_NAME["loom_memory_get"]
    assert original.input_model is MemoryGetInput and original.required_role is Role.VIEWER
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME, "loom_memory_get", replace(original, handler=handler)
    )
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    app = FastAPI()
    mcp.attach(app)
    monkeypatch.setattr(tools, "_app", app)
    return state


def test_native_read_descriptors_offer_row_identity_selectors():
    native = tools.Tools("acme", "n", "alice", "answer-row-read")
    descriptor = native.descriptors({"memory.get"})[0]["function"]["parameters"]
    assert {"agent", "user_id", "session_id"} <= descriptor["properties"].keys()
    assert not {"org", "namespace", "roles", "read_scope"} & descriptor["properties"].keys()


@pytest.mark.parametrize(
    "selectors",
    [
        {"user_id": "person-a"},
        {"session_id": "conversation-a"},
        {"user_id": "person-a", "session_id": "conversation-a"},
    ],
)
def test_native_row_selectors_reach_authenticated_memory_get(native_row_reader, selectors):
    async def scenario():
        native = tools.Tools("acme", "n", "alice", "answer-row-read")
        receipt = await native.call(
            "loom_memory_get",
            {"memory_key": "non-sentinel", "agent": "writer", **selectors},
            origin="model_tool",
        )
        assert receipt.result["found"] is True and receipt.result["value"] == "selected row"
        [inp] = native_row_reader.seen
        assert (inp.org, inp.namespace, inp.agent) == ("acme", "n", "writer")
        assert (inp.user_id, inp.session_id) == (
            selectors.get("user_id", "-"),
            selectors.get("session_id", "-"),
        )

    asyncio.run(scenario())


def test_native_row_selectors_keep_token_and_answer_ceilings(native_row_reader):
    from dataclasses import replace

    from server.core.errors import AuthInsufficientError

    state = native_row_reader
    state.principal = replace(state.principal, read_scope="own_user", read_user_id="person-a")
    arguments = {
        "memory_key": "non-sentinel",
        "agent": "writer",
        "user_id": "person-a",
        "session_id": "conversation-a",
    }

    async def scenario():
        native = tools.Tools("acme", "n", "alice", "answer-row-read")
        assert (await native.call("loom_memory_get", arguments, origin="host_context")).result[
            "found"
        ] is True
        for changed in ({"org": "other"}, {"namespace": "outside"}):
            with pytest.raises(AuthInsufficientError):
                await native.call("loom_memory_get", {**arguments, **changed}, origin="model_tool")
        with pytest.raises(AuthInsufficientError):
            await native.call("loom_memory_set", arguments, origin="model_tool")
        with pytest.raises(AssistantUpstreamError):
            await native.call(
                "loom_memory_get", {**arguments, "user_id": "other-user"}, origin="model_tool"
            )
        for org, namespace in (("acme", "outside"), ("other", "n")):
            outside = tools.Tools(org, namespace, "alice", "answer-outside")
            with pytest.raises(AssistantUpstreamError):
                await outside.call("loom_memory_get", arguments, origin="model_tool")
        state.grant = None
        with pytest.raises(AssistantUpstreamError):
            await tools.Tools("acme", "n", "alice", "answer-grant-lost").call(
                "loom_memory_get", arguments, origin="model_tool"
            )
        state.grant, state.revoked = Role.ADMIN, True
        with pytest.raises(AssistantUpstreamError):
            await tools.Tools("acme", "n", "alice", "answer-revoked").call(
                "loom_memory_get", arguments, origin="model_tool"
            )
        assert len(state.seen) == 1

    asyncio.run(scenario())


@pytest.fixture
def native_history_reader(native_row_reader, monkeypatch):
    from dataclasses import replace

    from server.auth.context import current_auth
    from server.memory.api.models_temporal import MemoryHistoryInput, MemoryHistoryOutput
    from server.transport import mcp_call

    state = native_row_reader
    state.history_seen = []

    async def handler(inp):
        assert isinstance(inp, MemoryHistoryInput)
        assert current_auth() == state.principal
        state.history_seen.append(inp)
        return MemoryHistoryOutput.model_validate(
            {
                "rows": [
                    {
                        "memory_id": "rate-limit",
                        "version": version,
                        "value": {"rate_limit": value},
                        "confidence": 1,
                        "created_at": "2024-01-01T00:00:00Z",
                        "agent": inp.agent,
                    }
                    for version, value in [(9007199254740995, 600), (9007199254740993, 300)]
                ]
            }
        )

    original = mcp_call.TOOLS_BY_NAME["loom_memory_history"]
    assert original.input_model is MemoryHistoryInput and original.required_role is Role.VIEWER
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME, "loom_memory_history", replace(original, handler=handler)
    )
    return state


def test_history_read_keeps_authenticated_scope_and_live_delegation(native_history_reader):
    from dataclasses import replace

    from server.core.errors import AuthInsufficientError

    state = native_history_reader
    state.principal = replace(state.principal, read_scope="own_user", read_user_id="person-a")
    arguments = {
        "memory_key": "rate-limit",
        "agent": "writer",
        "user_id": "person-a",
        "session_id": "conversation-a",
        "limit": 2,
    }

    async def scenario():
        native = tools.Tools("acme", "n", "alice", "history-answer")
        descriptor = native.descriptors({"memory.history"})[0]["function"]["parameters"]
        assert descriptor["properties"]["limit"]["minimum"] == 1
        assert descriptor["properties"]["limit"]["maximum"] == 500
        assert not {"org", "namespace", "roles", "read_scope"} & descriptor["properties"].keys()
        receipt = await native.call("loom_memory_history", arguments, origin="model_tool")
        assert [row["value"]["rate_limit"] for row in receipt.result["rows"]] == [600, 300]
        [inp] = state.history_seen
        assert (inp.org, inp.namespace, inp.agent, inp.user_id, inp.session_id) == (
            "acme",
            "n",
            "writer",
            "person-a",
            "conversation-a",
        )
        for changed in ({"org": "other"}, {"namespace": "outside"}):
            with pytest.raises(AuthInsufficientError):
                await native.call(
                    "loom_memory_history", {**arguments, **changed}, origin="model_tool"
                )
        with pytest.raises(AuthInsufficientError):
            await native.call("loom_memory_set", arguments, origin="model_tool")
        for changed in ({"user_id": "other-user"}, {"limit": 0}, {"limit": 501}):
            with pytest.raises(AssistantUpstreamError):
                await tools.Tools("acme", "n", "alice", "history-invalid").call(
                    "loom_memory_history", {**arguments, **changed}, origin="model_tool"
                )
        for org, namespace in (("other", "n"), ("acme", "outside")):
            with pytest.raises(AssistantUpstreamError):
                await tools.Tools(org, namespace, "alice", "history-outside").call(
                    "loom_memory_history", arguments, origin="model_tool"
                )
        state.grant = None
        with pytest.raises(AssistantUpstreamError):
            await tools.Tools("acme", "n", "alice", "history-grant-lost").call(
                "loom_memory_history", arguments, origin="model_tool"
            )
        state.grant, state.revoked = Role.ADMIN, True
        with pytest.raises(AssistantUpstreamError):
            await tools.Tools("acme", "n", "alice", "history-revoked").call(
                "loom_memory_history", arguments, origin="model_tool"
            )
        assert len(state.history_seen) == 1

    asyncio.run(scenario())


def test_history_calls_share_the_existing_answer_tool_ceiling(native_history_reader, monkeypatch):
    monkeypatch.setattr(settings, "assistant_max_tool_calls", 1)

    async def scenario():
        native = tools.Tools("acme", "n", "alice", "history-budget")
        arguments = {"memory_key": "rate-limit", "agent": "writer", "limit": 2}
        await native.call("loom_memory_history", arguments, origin="host_context")
        with pytest.raises(AssistantUpstreamError, match="tool budget"):
            await native.call(
                "loom_memory_history", {**arguments, "memory_key": "other"}, origin="model_tool"
            )
        assert len(native.receipts) == len(native_history_reader.history_seen) == 1

    asyncio.run(scenario())


def test_inventory_read_preserves_token_scope_and_private_members(native_row_reader, monkeypatch):
    from dataclasses import replace

    from server.auth.context import current_auth
    from server.core.errors import AuthInsufficientError
    from server.identity.api.models_inventory import (
        NamespaceInventoryInput,
        NamespaceInventoryOutput,
    )
    from server.identity.app.request_scope import request_scope
    from server.transport import mcp_call

    state = native_row_reader
    state.principal = replace(state.principal, read_scope="own_user", read_user_id="person-a")
    seen = []

    async def handler(inp):
        assert isinstance(inp, NamespaceInventoryInput) and current_auth() == state.principal
        scope = request_scope()
        assert (scope.scope, scope.user_id) == ("own_user", "person-a")
        assert (inp.org, inp.namespace, inp.population_limit) == ("acme", "n", 20)
        seen.append(inp)
        return NamespaceInventoryOutput.model_validate(
            {
                "scope": {
                    "org": inp.org,
                    "namespace": inp.namespace,
                    "read_scope": scope.scope,
                    "user_id": scope.user_id,
                    "session_id": None,
                },
                "sections": [],
                "capabilities": [],
                "computed_at": "2024-01-01T00:00:00Z",
                "populations": [
                    {
                        "population": "members",
                        "total": None,
                        "top": [],
                        "complete": False,
                        "reason": "membership unavailable",
                    },
                    {
                        "population": "agents",
                        "total": 1,
                        "top": [{"name": "scoped-bot", "rows": 1}],
                        "complete": True,
                    },
                ],
            }
        )

    original = mcp_call.TOOLS_BY_NAME["loom_namespace_inventory"]
    assert original.input_model is NamespaceInventoryInput and original.required_role is Role.VIEWER
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME, "loom_namespace_inventory", replace(original, handler=handler)
    )

    async def scenario():
        native = tools.Tools("acme", "n", "alice", "inventory-answer")
        descriptor = native.descriptors({"namespace.inventory"})[0]["function"]["parameters"]
        assert (
            not {"org", "namespace", "roles", "read_scope", "population_limit"}
            & descriptor["properties"].keys()
        )
        receipt = await native.call("loom_namespace_inventory", {}, origin="model_tool")
        assert receipt.result["scope"]["user_id"] == "person-a"
        members, agents = receipt.result["populations"]
        assert members["total"] is None and not members["top"] and not members["complete"]
        assert [row["name"] for row in agents["top"]] == ["scoped-bot"]
        for changed in ({"org": "other"}, {"namespace": "outside"}):
            with pytest.raises(AuthInsufficientError):
                await native.call("loom_namespace_inventory", changed, origin="model_tool")
        for changed in (
            {"read_scope": "namespace"},
            {"roles": ["admin"]},
            {"population_limit": 200},
        ):
            with pytest.raises(AssistantUpstreamError):
                await native.call("loom_namespace_inventory", changed, origin="model_tool")
        state.grant = None
        with pytest.raises(AssistantUpstreamError):
            await tools.Tools("acme", "n", "alice", "inventory-grant-lost").call(
                "loom_namespace_inventory", {}, origin="model_tool"
            )
        state.grant, state.revoked = Role.ADMIN, True
        with pytest.raises(AssistantUpstreamError):
            await tools.Tools("acme", "n", "alice", "inventory-revoked").call(
                "loom_namespace_inventory", {}, origin="model_tool"
            )
        assert len(seen) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["code.symbol", "code.search"])
def test_code_reads_resolve_plane_default_under_live_delegated_grants(
    native_row_reader, monkeypatch, operation
):
    from dataclasses import replace

    from server.auth import authz
    from server.core.errors import AuthInsufficientError
    from server.transport import mcp_call
    from tests.unit.mcp_probe import _Out

    state = native_row_reader
    monkeypatch.setattr(settings, "code_repo", "acme/widgets")
    monkeypatch.setattr(settings, "code_repos_json", "")
    seen = []

    async def handler(inp):
        from server.auth.context import current_auth
        from server.code.app.common import resolve_code_identity

        assert current_auth() == state.principal
        seen.append(resolve_code_identity(inp.org, inp.namespace))
        return _Out()

    name = "loom_" + operation.replace(".", "_")
    original = mcp_call.TOOLS_BY_NAME[name]
    assert original.required_role is Role.VIEWER and original.read_only
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME, name, replace(original, handler=handler, output_model=_Out)
    )
    raw = {"name": "Target"} if operation == "code.symbol" else {"query": "Target"}

    async def call(arguments=None):
        native = tools.Tools("acme", "n", "alice", "answer-code-read")
        result = await native.call(
            name, raw if arguments is None else arguments, origin="host_context"
        )
        assert result.arguments["org"] == "acme" and "namespace" not in result.arguments
        assert len(native.receipts) == 1
        return result

    async def scenario():
        # The answer token stays viewer on memory n. Existing code policy checks
        # its creator's live repository grant without adding a token namespace.
        assert state.principal.ns_roles == (("n", "viewer"),)
        assert await call()
        assert seen == [("acme", "code-acme")]
        assert await call({**raw, "namespace": "n"})
        assert seen == [("acme", "code-acme")] * 2
        for identity in [{"namespace": "code-acme"}, {"namespace": "other"}, {"org": "other"}]:
            with pytest.raises(AuthInsufficientError):
                await call({**raw, **identity})
        assert len(seen) == 2
        state.grant = None
        with pytest.raises(AssistantUpstreamError):
            await call()
        state.grant = Role.ADMIN
        state.revoked = True
        with pytest.raises(AssistantUpstreamError):
            await call()
        state.revoked = False
        # Restricted code still requires an explicit token claim: the answer's
        # memory-only claim and a creator grant cannot admit this read.
        monkeypatch.setattr(authz, "may_be_restricted", AsyncMock(return_value=True))
        with pytest.raises(AssistantUpstreamError):
            await call()
        monkeypatch.setattr(authz, "may_be_restricted", AsyncMock(return_value=False))
        state.principal = replace(
            state.principal, creator="", roles=(), ns_roles=(("n", "viewer"),)
        )
        monkeypatch.setattr(authz, "grant_role_for", AsyncMock(return_value=None))
        with pytest.raises(AssistantUpstreamError):
            await call()
        assert len(seen) == 2

    asyncio.run(scenario())
