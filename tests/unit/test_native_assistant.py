"""Native orchestration with replayed model turns and offline authenticated-tool seams."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from server.assistant.app import agent, evidence, runtime, service, tools
from server.assistant.app.tools import Receipt
from server.assistant.domain import context, history
from server.core.errors import AssistantUpstreamError, AuthInsufficientError
from server.llm import agent as model
from server.llm.agent_bedrock import BedrockStream, converse_body
from server.llm.agent_http import AnthropicStream, OpenAIStream
from server.observability import cost_accumulator
from server.settings import settings


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(settings, "assistant_enabled", True)
    monkeypatch.setattr(settings, "assistant_provider", "fake")
    monkeypatch.setattr(settings, "assistant_model", "")
    monkeypatch.setattr(settings, "assistant_price_model", "")
    monkeypatch.setattr(settings, "assistant_max_context_bytes", 200000)
    monkeypatch.setattr(settings, "assistant_max_tokens", 1000000)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda _: True)

    async def affordable(*args: Any) -> None:
        return None

    async def request(self: tools.Tools, name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, args))
        return {"results": []}

    monkeypatch.setattr(runtime, "_afford", affordable)
    monkeypatch.setattr(tools.Tools, "_request", request)
    return calls


def run(question: str = "our deployment policy") -> tuple[agent.State, list[dict[str, Any]]]:
    async def answer() -> tuple[agent.State, list[dict[str, Any]]]:
        state = agent.State()
        chunks = [
            frame
            async for frame in agent.run(
                "org",
                "ns",
                "answer-token",
                "viewer",
                [{"role": "user", "content": question}],
                state,
            )
        ]
        return state, chunks

    return asyncio.run(answer())


def test_host_context_precedes_the_first_model_request(
    offline: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Replay:
        async def stream(self, messages: list, descriptors: list, maximum: int, turn: model.Turn):
            assert [name for name, _ in offline] == ["loom_memory_search", "loom_knowledge_search"]
            assert [m["role"] for m in messages][-4:] == ["assistant", "tool", "assistant", "tool"]
            assert "answer-token" not in json.dumps(messages)
            assert len(descriptors) < len(tools.Tools("org", "ns", "t", "r").operations)
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = model.Usage(100, 10), "stop"
            turn.text = "Nothing stored in Loom answers this."
            yield turn.text

    monkeypatch.setattr(model, "model_for", lambda *_: Replay())
    state, chunks = run()
    assert state.turns == 1 and len(state.receipts) == 2
    assert {r.origin for r in state.receipts} == {"host_context"}
    assert chunks[-1]["usage"]["prompt_tokens"] == 100


@pytest.mark.parametrize(
    ("question", "operation"),
    [
        ("Where is function `run_agent` implemented?", "code.symbol"),
        ("How does repository code enforce isolation?", "code.search"),
        ("How do I use Loom?", "help.search"),
        ("How many calls ran?", "insights.catalog"),
    ],
)
def test_initial_context_routes_without_a_model(question: str, operation: str) -> None:
    assert context.initial(question)[0][0] == operation


def test_model_followup_is_adaptive_and_bounded(
    offline: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns: list[int] = []

    class Replay:
        async def stream(self, messages: list, descriptors: list, maximum: int, turn: model.Turn):
            turns.append(1)
            turn.sent = turn.usage_complete = True
            turn.usage = model.Usage(100, 10)
            if len(turns) == 1:
                turn.stop = "tool_calls"
                turn.calls = [model.ToolCall("next", "loom_memory_get", '{"memory_key":"policy"}')]
            else:
                turn.stop, turn.text = "stop", "Nothing stored in Loom answers this."
                yield turn.text

    monkeypatch.setattr(model, "model_for", lambda *_: Replay())
    state, _ = run()
    assert state.turns == 2 and state.input_tokens == 200
    assert state.receipts[-1].origin == "model_tool"
    assert offline[-1] == (
        "loom_memory_get",
        {"memory_key": "policy", "org": "org", "namespace": "ns"},
    )


@pytest.mark.parametrize("name", ["loom_assistant_ask", "loom_memory_set", "loom_token_mint"])
def test_model_cannot_expand_the_tool_roster(
    offline: list, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    class Replay:
        async def stream(self, messages: list, descriptors: list, maximum: int, turn: model.Turn):
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = model.Usage(100, 1), "tool_calls"
            turn.calls = [model.ToolCall("attack", name, "{}")]
            if False:
                yield ""

    monkeypatch.setattr(model, "model_for", lambda *_: Replay())
    with pytest.raises(AssistantUpstreamError, match="outside"):
        run()
    assert len(offline) == 2


def test_scope_and_duplicate_queries_refuse_before_dispatch(offline: list) -> None:
    async def scenario() -> None:
        tool = tools.Tools("org", "ns", "t", "r")
        with pytest.raises(AuthInsufficientError):
            await tool.call(
                "loom_memory_search", {"query": "x", "org": "other"}, origin="model_tool"
            )
        await tool.call(
            "loom_memory_search", {"query": "  Policy  ", "top_k": 3}, origin="host_context"
        )
        with pytest.raises(AssistantUpstreamError, match="repeated"):
            await tool.call(
                "loom_memory_search", {"query": "policy", "top_k": 10}, origin="model_tool"
            )

    asyncio.run(scenario())
    assert len(offline) == 1
    assert offline[0][1]["auto_touch"] is False


def test_cancellation_closes_the_model_and_accounts_once(
    offline: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class Slow:
        async def stream(self, messages: list, descriptors: list, maximum: int, turn: model.Turn):
            turn.sent = True
            try:
                yield "provisional"
                await asyncio.Event().wait()
            finally:
                closed.append(True)

    monkeypatch.setattr(model, "model_for", lambda *_: Slow())
    monkeypatch.setattr(
        service, "current_definition", lambda: service.Definition("fake", "claude-haiku-4-5")
    )

    async def scenario() -> None:
        acc = cost_accumulator.begin_op()
        state = agent.State()
        stream = agent.run(
            "org", "ns", "t", "viewer", [{"role": "user", "content": "policy"}], state
        )
        async with contextlib.aclosing(stream):
            await anext(stream)
            await anext(stream)
            assert (await anext(stream))["choices"][0]["delta"]["content"] == "provisional"
        assert acc.llm_calls == 1 and acc.cost_usd > 0 and acc.cost_estimated
        assert state.spent == acc.cost_usd and closed == [True]

    asyncio.run(scenario())


def test_deadline_joins_blocked_tool(offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    async def blocked(self: tools.Tools, *_: Any) -> dict:
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(tools.Tools, "_request", blocked)
    monkeypatch.setattr(settings, "assistant_answer_ttl_s", 0.02)
    ticket = runtime.Ticket("org", "ns", "t", 1, "viewer")

    async def scenario() -> None:
        with pytest.raises(AssistantUpstreamError, match="deadline"):
            async for _ in runtime.stream(
                ticket, [{"role": "user", "content": "policy"}], runtime.Answer()
            ):
                pass
        assert closed == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("assistant_max_context_bytes", 1),
        ("assistant_max_tokens", 1),
        ("assistant_answer_ceiling_usd", 0),
    ],
)
def test_budget_refusal_sends_no_model(
    offline: list, monkeypatch: pytest.MonkeyPatch, setting: str, value: int
) -> None:
    monkeypatch.setattr(settings, setting, value)
    monkeypatch.setattr(model, "model_for", lambda *_: pytest.fail("model sent"))
    if setting == "assistant_answer_ceiling_usd":
        monkeypatch.setattr(
            service, "current_definition", lambda: service.Definition("fake", "claude-haiku-4-5")
        )
    with pytest.raises(AssistantUpstreamError):
        run()


def _openai(text: str = "answer", stop: str = "stop", usage: bool = True) -> OpenAIStream:
    turn = model.Turn()
    parser = OpenAIStream(turn)
    parser.feed({"choices": [{"delta": {"content": text}, "finish_reason": stop}]})
    turn.text = text
    if usage:
        parser.feed(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 12,
                    "prompt_tokens_details": {"cached_tokens": 20},
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            }
        )
    return parser


def test_openai_requires_terminal_marker_and_preserves_usage() -> None:
    parser = _openai()
    with pytest.raises(AssistantUpstreamError, match="terminal"):
        parser.finish()
    parser.feed(None)
    parser.finish()
    assert parser.turn.usage == model.Usage(80, 12, cache_read=20, reasoning=5)


@pytest.mark.parametrize("stop", ["length", "content_filter", "refusal", "", "unknown"])
def test_provider_partial_stops_refuse_completion(stop: str) -> None:
    parser = _openai(stop=stop)
    parser.feed(None)
    with pytest.raises(AssistantUpstreamError):
        parser.finish()


def test_missing_or_repeated_usage_fails_closed() -> None:
    parser = _openai(usage=False)
    parser.feed(None)
    with pytest.raises(AssistantUpstreamError, match="usage"):
        parser.finish()
    parser = _openai()
    with pytest.raises(AssistantUpstreamError, match="repeated usage"):
        parser.feed({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 1}})


def test_anthropic_stream_preserves_cache_write_ttls() -> None:
    parser = AnthropicStream(model.Turn())
    parser.feed(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "cache_creation": {"ephemeral_1h_input_tokens": 10},
                }
            },
        }
    )
    parser.feed(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    )
    parser.turn.text = parser.feed(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "answer"},
        }
    )
    parser.feed({"type": "content_block_stop", "index": 0})
    parser.feed(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 10},
        }
    )
    parser.feed({"type": "message_stop"})
    parser.finish()
    assert parser.turn.usage == model.Usage(100, 10, 20, 20, 10)


def test_bedrock_stream_and_tool_results_use_converse_contract() -> None:
    parser = BedrockStream(model.Turn())
    parser.feed("messageStart", {"role": "assistant"})
    parser.turn.text = parser.feed(
        "contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "answer"}}
    )
    parser.feed("contentBlockStop", {"contentBlockIndex": 0})
    parser.feed("messageStop", {"stopReason": "end_turn"})
    parser.feed(
        "metadata", {"usage": {"inputTokens": 100, "outputTokens": 10, "cacheReadInputTokens": 20}}
    )
    parser.finish()
    assert parser.turn.usage == model.Usage(100, 10, 20)
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [model.ToolCall("a", "tool", "{}").wire()],
        },
        {"role": "tool", "tool_call_id": "a", "content": "result"},
    ]
    body = converse_body(messages, [], 100)
    assert body["messages"][-1]["content"][0]["toolResult"]["toolUseId"] == "a"
    assert "temperature" not in json.dumps(body)


@pytest.mark.parametrize("first", ["tool", "text"])
@pytest.mark.parametrize("reused", ["tool_start", "text_delta", "tool_delta", "text_start"])
def test_bedrock_rejects_reused_closed_content_block_indices(first: str, reused: str) -> None:
    parser = BedrockStream(model.Turn())

    def feed(kind: str, frame: dict[str, Any]) -> None:
        parser.turn.text += parser.feed(kind, frame)

    feed("messageStart", {"role": "assistant"})
    if first == "tool":
        feed(
            "contentBlockStart",
            {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "a", "name": "tool_a"}}},
        )
        feed("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{}"}}})
    else:
        feed("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "first"}})
    feed("contentBlockStop", {"contentBlockIndex": 0})
    original_call = parser.calls.get(0)
    with pytest.raises(AssistantUpstreamError):
        if reused == "tool_start":
            feed(
                "contentBlockStart",
                {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "b", "name": "tool_b"}},
                },
            )
            feed(
                "contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{}"}}}
            )
        elif reused == "text_delta":
            feed("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "second"}})
        elif reused == "tool_delta":
            feed(
                "contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{}"}}}
            )
        else:
            feed("contentBlockStart", {"contentBlockIndex": 0, "start": {"text": ""}})
        feed("contentBlockStop", {"contentBlockIndex": 0})
        feed("messageStop", {"stopReason": "tool_use" if parser.calls else "end_turn"})
        feed("metadata", {"usage": {"inputTokens": 100, "outputTokens": 10}})
        parser.finish()
    assert parser.calls.get(0) is original_call
    assert not parser.turn.calls and not parser.turn.usage_complete


@pytest.mark.parametrize("index", [0, 7])
def test_bedrock_preserves_implicit_text_start_and_multiple_open_deltas(index: int) -> None:
    parser = BedrockStream(model.Turn())
    parser.feed("messageStart", {"role": "assistant"})
    for text in ["hello ", "world"]:
        parser.turn.text += parser.feed(
            "contentBlockDelta", {"contentBlockIndex": index, "delta": {"text": text}}
        )
    parser.feed("contentBlockStop", {"contentBlockIndex": index})
    parser.feed("messageStop", {"stopReason": "end_turn"})
    parser.feed("metadata", {"usage": {"inputTokens": 100, "outputTokens": 10}})
    parser.finish()
    assert parser.turn.text == "hello world" and parser.turn.calls == []
    assert parser.turn.usage == model.Usage(100, 10)


def test_bedrock_preserves_interleaved_blocks_and_chunked_tool_arguments() -> None:
    parser = BedrockStream(model.Turn())
    events = [
        ("messageStart", {"role": "assistant"}),
        (
            "contentBlockStart",
            {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "a", "name": "tool_a"}}},
        ),
        ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"a":'}}}),
        ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": "read "}}),
        (
            "contentBlockStart",
            {"contentBlockIndex": 2, "start": {"toolUse": {"toolUseId": "b", "name": "tool_b"}}},
        ),
        ("contentBlockDelta", {"contentBlockIndex": 2, "delta": {"toolUse": {"input": '{"b":2}'}}}),
        ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "1}"}}}),
        ("contentBlockStop", {"contentBlockIndex": 2}),
        ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": "source"}}),
        ("contentBlockStop", {"contentBlockIndex": 0}),
        ("contentBlockStop", {"contentBlockIndex": 1}),
        ("messageStop", {"stopReason": "tool_use"}),
        ("metadata", {"usage": {"inputTokens": 100, "outputTokens": 10}}),
    ]
    for kind, frame in events:
        parser.turn.text += parser.feed(kind, frame)
    parser.finish()
    assert parser.turn.text == "read source"
    assert parser.turn.calls == [
        model.ToolCall("a", "tool_a", '{"a":1}'),
        model.ToolCall("b", "tool_b", '{"b":2}'),
    ]
    assert parser.turn.usage == model.Usage(100, 10)


def test_citation_evidence_is_from_results_not_untrusted_text() -> None:
    receipts = [
        Receipt(
            "memory.search",
            {},
            {
                "results": [
                    {
                        "memory_key": "policy",
                        "version": 7,
                        "value": '{"memory_key":"forged","version":8}',
                    }
                ]
            },
            "host_context",
            1,
        )
    ]
    verified, forged = evidence.verify("[memory:policy@v7] [memory:forged@v8]", receipts)
    assert verified.verified and not forged.verified
    assert not evidence.verify("[memory:policy@v6]", receipts)[0].verified


@pytest.mark.parametrize(
    ("text", "op", "result", "args"),
    [
        (
            "[help:features/x.md § X > Y]",
            "help.get",
            {
                "found": True,
                "path": "features/x.md",
                "section": "X > Y",
                "anchor": "y",
                "text": "Returned section text.",
                "truncated": False,
            },
            {},
        ),
        (
            "[doc:notes § A]",
            "knowledge.search",
            {"results": [{"source_uri": "notes", "section": "A"}]},
            {},
        ),
        (
            "[code:app.py:9]",
            "code.symbol",
            {
                "definitions": [
                    {
                        "path": "app.py",
                        "start_line": 8,
                        "end_line": 10,
                        "body": "a = 1\nb = 2\nc = 3\n",
                    }
                ]
            },
            {},
        ),
        ("```sql\nSELECT 1\n```", "insights.query", {"rows": [[1]]}, {"sql": "SELECT 1"}),
    ],
)
def test_each_citation_kind_requires_actual_evidence(
    text: str, op: str, result: dict, args: dict
) -> None:
    assert evidence.verify(text, [Receipt(op, args, result, "model_tool", 1)])[0].verified
    assert not evidence.verify(text, [Receipt("memory.search", {}, {}, "host_context", 1)])[
        0
    ].verified


def test_export_conversion_preserves_identity_and_owner_scope() -> None:
    export = {
        "conversationId": "c1",
        "user": "owner1",
        "messages": [
            {"messageId": "m1", "parentMessageId": "", "isCreatedByUser": True, "text": "question"},
            {
                "messageId": "m2",
                "parentMessageId": "m1",
                "isCreatedByUser": False,
                "text": "answer",
            },
        ],
    }
    archive = history.convert(
        export, subject="alice", org="org", namespace="ns", owner_bindings={"owner1": "alice"}
    )
    assert archive["conversation_id"] == "c1"
    assert [m["id"] for m in archive["messages"]] == ["m1", "m2"]
    assert history.history(archive, subject="alice", org="org", namespace="ns", leaf="m2") == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    with pytest.raises(ValueError, match="owner"):
        history.convert(
            export, subject="bob", org="org", namespace="ns", owner_bindings={"owner1": "alice"}
        )
    with pytest.raises(ValueError, match="scope"):
        history.history(archive, subject="alice", org="org", namespace="other", leaf="m2")


@pytest.mark.parametrize(("uncached", "cached"), [(100, 20), (10, 100)])
def test_bedrock_cache_counters_are_additive(uncached, cached):
    parser = BedrockStream(model.Turn())
    parser.feed("messageStart", {"role": "assistant"})
    parser.turn.text = parser.feed(
        "contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "answer"}}
    )
    parser.feed("contentBlockStop", {"contentBlockIndex": 0})
    parser.feed("messageStop", {"stopReason": "end_turn"})
    parser.feed(
        "metadata",
        {"usage": {"inputTokens": uncached, "outputTokens": 10, "cacheReadInputTokens": cached}},
    )
    parser.finish()
    assert parser.turn.usage.input == uncached
    assert parser.turn.usage.total_input == uncached + cached


def test_missing_document_cannot_validate_a_citation():
    receipts = [
        Receipt(
            "knowledge.get",
            {"source_uri": "missing"},
            {"found": False, "source_uri": "missing"},
            "model_tool",
            1,
        )
    ]
    assert not evidence.verify("[doc:missing]", receipts)[0].verified


@pytest.mark.parametrize(
    "change",
    [
        {"messageId": "m1", "parentMessageId": "missing", "text": "x", "role": "user"},
        {"messageId": "m1", "parentMessageId": "", "text": "x", "role": "tool"},
        {
            "messageId": "m1",
            "parentMessageId": "",
            "text": "x",
            "role": "user",
            "files": ["private"],
        },
        {"messageId": "m1", "parentMessageId": "", "text": "x" * 20001, "role": "user"},
    ],
)
def test_export_refuses_unbounded_or_nontext_state(change):
    export = {"conversationId": "c1", "user": "owner", "messages": [change]}
    with pytest.raises(ValueError):
        history.convert(
            export, subject="alice", org="org", namespace="ns", owner_bindings={"owner": "alice"}
        )


def test_model_cannot_select_a_tool_model_or_price(offline):
    async def scenario():
        native = tools.Tools("org", "ns", "t", "r")
        with pytest.raises(AssistantUpstreamError, match="bounded schema"):
            await native.call(
                "loom_memory_search",
                {"query": "policy", "rerank_model": "other-model"},
                origin="model_tool",
            )

    asyncio.run(scenario())
    assert offline == []


def test_bedrock_cache_write_ttls_are_preserved():
    from server.llm.agent_bedrock import _usage

    usage = _usage(
        {
            "inputTokens": 10,
            "outputTokens": 2,
            "cacheWriteInputTokens": 100,
            "cacheDetails": [{"ttl": "1h", "inputTokens": 60}, {"ttl": "5m", "inputTokens": 40}],
        }
    )
    assert usage == model.Usage(10, 2, 0, 40, 60)
    with pytest.raises(AssistantUpstreamError, match="breakdown"):
        _usage({"inputTokens": 10, "outputTokens": 2, "cacheWriteInputTokens": 100})


def test_reported_usage_is_charged_once_even_when_the_provider_cuts_the_answer(
    offline, monkeypatch
):
    class Cut:
        async def stream(self, messages, descriptors, maximum, turn):
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = model.Usage(100, 10, 20, 5, 2), "length"
            turn.text = "partial"
            yield turn.text

    monkeypatch.setattr(model, "model_for", lambda *_: Cut())
    monkeypatch.setattr(
        service, "current_definition", lambda: service.Definition("fake", "claude-haiku-4-5")
    )

    async def scenario():
        acc = cost_accumulator.begin_op()
        state = agent.State()
        with pytest.raises(AssistantUpstreamError):
            [
                frame
                async for frame in agent.run(
                    "org", "ns", "t", "viewer", [{"role": "user", "content": "policy"}], state
                )
            ]
        assert acc.llm_calls == 1 and acc.llm_input_tokens == 127 and acc.llm_output_tokens == 10
        assert not acc.cost_estimated
        assert acc.cost_usd == pytest.approx(state.spent)

    asyncio.run(scenario())


def test_mirror_marker_precedes_native_producer_work(monkeypatch):
    called = []

    async def producer(*args):
        called.append(True)
        yield {"choices": []}

    monkeypatch.setattr(agent, "run", producer)

    async def scenario():
        stream = runtime.stream(
            runtime.Ticket("org", "ns", "t", 1, "viewer", True),
            [{"role": "user", "content": "policy"}],
            runtime.Answer(),
        )
        assert await anext(stream) == b'data: {"mirrored":true}\n\n'
        assert called == []
        await stream.aclose()

    asyncio.run(scenario())


def test_unverified_citation_withholds_terminal_done(offline, monkeypatch):
    class Forged:
        async def stream(self, messages, descriptors, maximum, turn):
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = model.Usage(100, 10), "stop"
            turn.text = "[memory:unread@v1]"
            yield turn.text

    monkeypatch.setattr(model, "model_for", lambda *_: Forged())

    async def scenario():
        received = []
        with pytest.raises(AssistantUpstreamError, match="unverified"):
            async for frame in runtime.stream(
                runtime.Ticket("org", "ns", "t", 1, "viewer"),
                [{"role": "user", "content": "policy"}],
                runtime.Answer(),
            ):
                received.append(frame)
        assert b"data: [DONE]\n\n" not in received

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "question", ["Who calls `authorize_request`?", "Where is `app/auth.py:12` implemented?"]
)
def test_named_code_routing_recognizes_definition_and_caller_questions(question):
    assert context.initial(question)[0][0] == "code.symbol"


def test_greeting_does_not_force_context_calls():
    assert context.initial("Hello!") == []


def test_model_step_limit_refuses_a_followup_before_dispatch(offline, monkeypatch):
    monkeypatch.setattr(settings, "assistant_max_steps", 1)

    class Followup:
        async def stream(self, messages, descriptors, maximum, turn):
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = model.Usage(100, 10), "tool_calls"
            turn.calls = [model.ToolCall("next", "loom_memory_search", '{"query":"another"}')]
            yield ""

    monkeypatch.setattr(model, "model_for", lambda *_: Followup())
    with pytest.raises(AssistantUpstreamError, match="step budget"):
        run()
    assert [name for name, _ in offline] == ["loom_memory_search", "loom_knowledge_search"]


def test_tool_limit_counts_host_context_before_any_model(offline, monkeypatch):
    monkeypatch.setattr(settings, "assistant_max_tool_calls", 1)
    monkeypatch.setattr(model, "model_for", lambda *_: pytest.fail("model sent"))
    with pytest.raises(AssistantUpstreamError, match="tool budget"):
        run()
    assert [name for name, _ in offline] == ["loom_memory_search"]


def test_reported_output_above_the_token_cap_refuses(offline, monkeypatch):
    monkeypatch.setattr(settings, "assistant_max_output_tokens", 64)

    class Oversized:
        async def stream(self, messages, descriptors, maximum, turn):
            assert maximum == 64
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = model.Usage(100, 65), "stop"
            turn.text = "answer"
            yield turn.text

    monkeypatch.setattr(model, "model_for", lambda *_: Oversized())
    with pytest.raises(AssistantUpstreamError, match="output token cap"):
        run()


def test_provisional_text_above_the_character_cap_withholds_done(offline, monkeypatch):
    monkeypatch.setattr(settings, "assistant_max_output_chars", 1000)

    class Oversized:
        async def stream(self, messages, descriptors, maximum, turn):
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = model.Usage(100, 10), "stop"
            turn.text = "x" * 1001
            yield turn.text

    monkeypatch.setattr(model, "model_for", lambda *_: Oversized())

    async def scenario():
        ticket = runtime.Ticket("org", "ns", "t", 1, "viewer")
        seen = []
        with pytest.raises(AssistantUpstreamError, match="output bound"):
            async for chunk in runtime.stream(
                ticket, [{"role": "user", "content": "policy"}], runtime.Answer()
            ):
                seen.append(chunk)
        assert b"data: [DONE]\n\n" not in seen

    asyncio.run(scenario())


def test_tool_result_above_the_character_cap_refuses(monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setattr(settings, "assistant_max_tool_result_chars", 1000)
    app = FastAPI()

    @app.post("/mcp")
    async def response():
        return {"result": {"structuredContent": {"data": "x" * 1001}}}

    monkeypatch.setattr(tools, "_app", app)

    async def scenario():
        with pytest.raises(AssistantUpstreamError, match="response bound"):
            await tools.Tools("org", "ns", "t", "run")._request("loom_memory_get", {})

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["code.symbol", "code.search"])
@pytest.mark.parametrize(
    ("change", "line", "verified"),
    [
        ({}, 9, True),
        ({"granularity": "file"}, 9, True),
        ({"body": ""}, 9, False),
        ({"body": None}, 9, False),
        ({"body": ["a = 1", "b = 2", "c = 3"]}, 9, False),
        ({"body": "a = 1\n"}, 9, False),
        ({"body": "a = 1\nb = 2\nc = 3\nextra = 4\n"}, 9, False),
        ({"elided": True, "body": ""}, 9, False),
        ({"elided": True}, 9, False),
        ({"granularity": "card"}, 9, False),
        ({"granularity": "skeleton", "body": "class A:\n    # body elided\n    pass\n"}, 9, False),
        ({"kind": "class_skeleton"}, 9, False),
        ({"granularity": "unknown"}, 9, False),
        ({"start_line": True}, 9, False),
        ({"start_line": 0}, 9, False),
        ({"end_line": "10"}, 9, False),
        ({"end_line": 100}, 50, False),
        ({}, 7, False),
        ({}, 11, False),
    ],
)
def test_code_citations_require_the_complete_delivered_source_span(
    operation, change, line, verified
):
    unit = {"path": "app.py", "start_line": 8, "end_line": 10, "body": "a = 1\nb = 2\nc = 3\n"}
    unit.update(change)
    container = "definitions" if operation == "code.symbol" else "results"
    receipt = Receipt(operation, {}, {container: [unit]}, "model_tool", 1)
    (citation,) = evidence.verify(f"[code:app.py:{line}]", [receipt])
    assert citation.verified is verified


@pytest.mark.parametrize("operation", ["code.symbol", "code.search"])
def test_code_citation_metadata_without_a_body_does_not_count_as_a_source_read(operation):
    from server.code.domain.units import CodeUnit

    unit = CodeUnit(path="app.py", start_line=8, end_line=10, elided=True, granularity="card")
    delivered = unit.model_dump()
    assert delivered["body"] == "" and delivered["elided"] is True
    container = "definitions" if operation == "code.symbol" else "results"
    receipt = Receipt(operation, {}, {container: [delivered]}, "model_tool", 1)
    assert not evidence.verify("[code:app.py:9]", [receipt])[0].verified


@pytest.mark.parametrize("origin", ["host_context", "model_tool"])
def test_tool_failure_records_no_receipt_and_withholds_done(offline, monkeypatch, origin):
    attempted = []
    turns = []

    async def request(self, name, args):
        attempted.append(name)
        if origin == "host_context" or name == "loom_memory_get":
            raise AssistantUpstreamError("injected tool failure")
        return {"results": []}

    class Followup:
        async def stream(self, messages, descriptors, maximum, turn):
            turns.append(1)
            turn.sent = turn.usage_complete = True
            turn.usage, turn.stop = model.Usage(100, 1), "tool_calls"
            turn.calls = [model.ToolCall("next", "loom_memory_get", '{"memory_key":"policy"}')]
            if False:
                yield ""

    monkeypatch.setattr(tools.Tools, "_request", request)
    monkeypatch.setattr(model, "model_for", lambda *_: Followup())

    async def scenario():
        answer = runtime.Answer()
        received = []
        with pytest.raises(AssistantUpstreamError, match="injected tool failure"):
            async for frame in runtime.stream(
                runtime.Ticket("org", "ns", "t", 1, "viewer"),
                [{"role": "user", "content": "policy"}],
                answer,
                live=True,
            ):
                received.append(frame)
        assert b"data: [DONE]\n\n" not in received
        assert answer.state is not None
        expected = [] if origin == "host_context" else ["memory.search", "knowledge.search"]
        assert [receipt.op for receipt in answer.state.receipts] == expected
        labels = ["Searched memories", "Searched documents"][: len(expected)]
        assert runtime._steps(answer) == [
            {"op": op, "text": label, "ok": True}
            for op, label in zip(expected, labels, strict=True)
        ]

    asyncio.run(scenario())
    assert len(attempted) == (1 if origin == "host_context" else 3)
    assert len(turns) == (0 if origin == "host_context" else 1)


@pytest.mark.parametrize(
    ("reads", "text", "expected"),
    [
        (
            [("memory.search", {"results": []}), ("namespace.inventory", {})],
            "A claim from the inventory.",
            "unchecked",
        ),
        (
            [
                ("memory.search", {"results": [{"memory_key": "policy"}]}),
                ("namespace.inventory", {}),
            ],
            "A claim without its memory citation.",
            "unsupported",
        ),
        (
            [("memory.get", {"found": False}), ("namespace.inventory", {})],
            "A claim from the inventory.",
            "unchecked",
        ),
        (
            [("memory.get", {"found": True}), ("namespace.inventory", {})],
            "A claim without its memory citation.",
            "unsupported",
        ),
        ([("mentor.guide", {})], "Write one claim per key.", "none"),
        ([("namespace.inventory", {})], "An uncitable count.", "unchecked"),
        (
            [("memory.get", {"found": True, "canonical_key": "policy", "version": 7})],
            "A cited claim [memory:policy@v7].",
            "supported",
        ),
    ],
)
def test_successful_receipt_support_uses_delivered_found_signals(reads, text, expected):
    receipts = [Receipt(op, {}, result, "model_tool", 1) for op, result in reads]
    citations = evidence.verify(text, receipts)
    assert evidence.support(text, receipts, citations) == expected
