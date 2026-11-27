"""Tool-owned provider sends spend an answer's allowance before dispatch."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from server.assistant.app import agent, runtime, tools
from server.clients import _http
from server.core.errors import AssistantUpstreamError
from server.observability import cost_accumulator
from server.settings import settings


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    async def affordable(*_: Any) -> None:
        pass

    async def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(runtime, "_afford", affordable)
    monkeypatch.setattr(settings, "assistant_answer_ceiling_usd", 0.000001)
    monkeypatch.setattr(settings, "inference_customer_header", "")
    monkeypatch.setattr(_http, "refuse_off_gateway", lambda _: None)

    async def request(self: tools.Tools, *_: Any, **__: Any) -> dict[str, Any]:
        async with _http._build(
            transport=httpx.MockTransport(transport),
            event_hooks={"request": [_http._on_request]},
        ) as client:
            await client.post(
                "https://provider.invalid/embeddings",
                json={"model": "text-embedding-3-small", "input": "x" * 1000},
            )
        cost_accumulator.report_embed("text-embedding-3-small", 1000)
        return {"results": []}

    monkeypatch.setattr(tools.Tools, "_request", request)
    return sent


@pytest.mark.parametrize("origin", ["host_context", "model_tool"])
def test_tool_refuses_unaffordable_embedding_before_dispatch(boundary: list, origin: str) -> None:
    async def check() -> None:
        state = agent.State()
        with pytest.raises(AssistantUpstreamError, match="cost budget"):
            await agent._call(
                tools.Tools("org", "ns", "t", "r"),
                "loom_memory_search",
                {"query": "policy"},
                origin=origin,
                state=state,
            )
        assert boundary == []
        assert state.spent == 0

    asyncio.run(check())


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("embeddings", {"model": "unpriced", "input": "query"}),
        ("rerank", {"model": "gpt-4o-mini", "query": "query"}),
        ("chat/completions", {"model": "gpt-4o-mini", "messages": []}),
        ("messages", {"model": "claude-sonnet-4-6", "max_tokens": False}),
    ],
)
def test_unpriced_or_unbounded_send_is_refused(path: str, payload: dict) -> None:
    from server.observability import provider_budget

    with pytest.raises(AssistantUpstreamError, match="cost budget"):
        with provider_budget.limited(1) as budget:
            provider_budget.sending(
                httpx.Request("POST", "https://provider.invalid/" + path, json=payload)
            )
    assert budget.reserved == 0


@pytest.mark.parametrize("failure", [False, True, "cancel", "after_charge"])
def test_allowed_work_accounts_once_and_missing_usage_holds_reservation(
    boundary: list, monkeypatch: pytest.MonkeyPatch, failure: bool | str
) -> None:
    from server.observability import provider_budget

    async def check() -> None:
        parent = cost_accumulator.begin_op()
        state = agent.State()
        children = []
        monkeypatch.setattr(settings, "assistant_answer_ceiling_usd", 1)

        async def request(self: tools.Tools, *_: Any, **__: Any) -> dict:
            child = cost_accumulator.begin_op()
            children.append(child)
            req = httpx.Request(
                "POST",
                "https://provider.invalid/embeddings",
                json={"model": "text-embedding-3-small", "input": "query"},
            )
            provider_budget.sending(req)
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure is True:
                raise ValueError("lost usage")
            cost_accumulator.report_embed("text-embedding-3-small", 5)
            if failure == "after_charge":
                raise ValueError("tool failed after charge")
            return {"results": []}

        monkeypatch.setattr(tools.Tools, "_request", request)
        try:
            await agent._call(
                tools.Tools("org", "ns", "t", "r"),
                "loom_memory_search",
                {"query": "policy"},
                origin="model_tool",
                state=state,
            )
        except (ValueError, asyncio.CancelledError):
            assert failure
        assert state.spent > 0
        if failure is True or failure == "cancel":
            assert parent.cost_usd == state.spent
            assert parent.cost_estimated
            assert children[0].cost_usd == 0
        else:
            assert parent.cost_usd == 0
            assert children[0].cost_usd == cost_accumulator.embed_cost_usd(
                "text-embedding-3-small", 5
            )
            assert state.spent >= children[0].cost_usd
        # The cancelled or failed answer cannot leak its ceiling into other work.
        provider_budget.sending(httpx.Request("POST", "https://provider.invalid/unpriced", json={}))

    asyncio.run(check())


def test_retries_and_child_tasks_share_reservations_but_answers_are_isolated() -> None:
    from server.observability import provider_budget

    async def check() -> None:
        req = httpx.Request(
            "POST",
            "https://provider.invalid/embeddings",
            json={"model": "text-embedding-3-small", "input": "query"},
        )
        _, price = provider_budget._price(req)
        ready = asyncio.Event()
        release = asyncio.Event()

        async def bounded() -> None:
            with pytest.raises(AssistantUpstreamError):
                with provider_budget.limited(price * 1.5) as budget:

                    async def first() -> None:
                        provider_budget.sending(req)
                        ready.set()
                        await release.wait()

                    task = asyncio.create_task(first())
                    await ready.wait()
                    release.set()
                    await task
                    provider_budget.sending(req)
            assert budget.reserved == price

        async def separate() -> None:
            await ready.wait()
            with provider_budget.limited(price * 3) as budget:
                provider_budget.sending(req)
                provider_budget.sending(req)
            assert budget.reserved == price * 2

        await asyncio.gather(bounded(), separate())

    asyncio.run(check())


def test_fail_open_tool_cannot_swallow_a_refused_provider_send(
    boundary: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.observability import provider_budget

    async def request(self: tools.Tools, *_: Any, **__: Any) -> dict:
        try:
            provider_budget.sending(
                httpx.Request("POST", "https://provider.invalid/rerank", json={"model": "unknown"})
            )
        except AssistantUpstreamError:
            pass
        return {"results": []}

    monkeypatch.setattr(tools.Tools, "_request", request)

    async def check() -> None:
        with pytest.raises(AssistantUpstreamError, match="cost budget"):
            await agent._call(
                tools.Tools("org", "ns", "t", "r"),
                "loom_memory_search",
                {"query": "policy"},
                origin="host_context",
                state=agent.State(),
            )

    asyncio.run(check())


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("chat/completions", {"model": "gpt-4o-mini", "messages": [], "max_completion_tokens": 12}),
        ("v1/messages", {"model": "claude-sonnet-4-6", "messages": [], "max_tokens": 12}),
        (
            "responses",
            {
                "model": "gpt-4o-mini",
                "input": [{"role": "user", "content": "query"}],
                "store": False,
                "max_output_tokens": 16,
            },
        ),
    ],
)
def test_supported_protocol_reserves_before_each_actual_http_dispatch(
    path: str, payload: dict
) -> None:
    from server.observability import provider_budget

    async def check() -> None:
        sent = []

        async def transport(request: httpx.Request) -> httpx.Response:
            assert budget.reserved > 0
            sent.append(request)
            return httpx.Response(200)

        async with _http._build(
            transport=httpx.MockTransport(transport), event_hooks={"request": [_http._on_request]}
        ) as client:
            with provider_budget.limited(1) as budget:
                await client.post("https://provider.invalid/" + path, json=payload)
        assert len(sent) == 1 and len(budget.sends) == 1

    asyncio.run(check())


def test_reported_charge_beyond_bound_is_retained_and_refuses_answer() -> None:
    from server.observability import provider_budget

    with pytest.raises(AssistantUpstreamError):
        with provider_budget.limited(1) as budget:
            budget.reserve("gpt-4o-mini", 0.1)
            cost_accumulator.report_charge("gpt-4o-mini", 0.2)
    assert budget.cost_usd == 0.2


def test_concurrent_provider_usage_binds_to_its_own_send() -> None:
    from server.observability import provider_budget

    async def check() -> None:
        first_sent = asyncio.Event()
        second_sent = asyncio.Event()
        acc = cost_accumulator.begin_op()

        async def call(size: int) -> None:
            provider_budget.sending(
                httpx.Request(
                    "POST",
                    "https://provider.invalid/embeddings",
                    json={"model": "text-embedding-3-small", "input": "x" * size},
                )
            )
            if size == 1000:
                first_sent.set()
                await second_sent.wait()
            else:
                second_sent.set()
            cost_accumulator.report_embed("text-embedding-3-small", size)

        with provider_budget.limited(1) as budget:
            large = asyncio.create_task(call(1000))
            await first_sent.wait()
            small = asyncio.create_task(call(1))
            await asyncio.gather(large, small)
        assert not budget.refused
        assert all(sent.reported for sent in budget.sends)
        assert not acc.cost_estimated
        assert acc.cost_usd == (
            cost_accumulator.embed_cost_usd("text-embedding-3-small", 1000)
            + cost_accumulator.embed_cost_usd("text-embedding-3-small", 1)
        )

    asyncio.run(check())


def test_host_plan_refuses_paid_context_before_first_model_turn(
    boundary: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "assistant_enabled", True)
    monkeypatch.setattr(settings, "assistant_provider", "fake")
    monkeypatch.setattr(settings, "assistant_model", "")
    monkeypatch.setattr(settings, "assistant_price_model", "")
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda _: True)

    async def check() -> None:
        state = agent.State()
        with pytest.raises(AssistantUpstreamError, match="cost budget"):
            _ = [
                frame
                async for frame in agent.run(
                    "org",
                    "ns",
                    "t",
                    "viewer",
                    [{"role": "user", "content": "our deployment policy"}],
                    state,
                )
            ]
        assert state.turns == 0 and state.receipts == [] and state.spent == 0
        assert boundary == []

    asyncio.run(check())


def test_only_authentication_exchange_is_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.llm import gateway
    from server.observability import provider_budget

    monkeypatch.setattr(gateway, "exchanging", lambda: True)
    monkeypatch.setattr(settings, "inference_token_url", "https://auth.invalid/token")
    with provider_budget.limited(0) as budget:
        provider_budget.sending(
            httpx.Request(
                "POST", "https://auth.invalid/token", content=b"grant_type=client_credentials"
            )
        )
    assert budget.sends == []
    with pytest.raises(AssistantUpstreamError):
        with provider_budget.limited(0):
            provider_budget.sending(httpx.Request("POST", "https://auth.invalid/other", json={}))


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("embeddings", {"model": "gpt-4o-mini", "input": "query"}),
        (
            "chat/completions",
            {"model": "text-embedding-3-small", "messages": [], "max_tokens": 100},
        ),
    ],
)
def test_a_price_for_another_protocol_never_admits_a_send(path: str, payload: dict) -> None:
    from server.observability import provider_budget

    with pytest.raises(AssistantUpstreamError):
        with provider_budget.limited(1) as budget:
            provider_budget.sending(
                httpx.Request("POST", "https://provider.invalid/" + path, json=payload)
            )
    assert budget.sends == []


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "model/cohere.embed-v4%3A0/invoke",
            {
                "texts": ["query"],
                "input_type": "search_query",
                "embedding_types": ["float"],
                "output_dimension": 256,
                "truncate": "NONE",
            },
        ),
        ("model/text-embedding-3-small/invoke", {"texts": ["query"]}),
        (
            "model/claude-sonnet-4-6/converse",
            {"messages": [], "inferenceConfig": {"maxTokens": 12}},
        ),
        (
            "v2/rerank",
            {
                "model": "rerank-english-v3.0",
                "query": "query",
                "documents": ["one", "two"],
                "top_n": 2,
                "return_documents": False,
            },
        ),
        (
            "rerank",
            {"query": "query", "prompt_template": "", "candidates": [{"content_excerpt": "one"}]},
        ),
    ],
)
def test_routes_without_a_price_contract_refuse_before_dispatch(path: str, payload: dict) -> None:
    from server.observability import provider_budget

    with pytest.raises(AssistantUpstreamError):
        with provider_budget.limited(1) as budget:
            provider_budget.sending(
                httpx.Request("POST", "https://provider.invalid/" + path, json=payload)
            )
    assert budget.sends == []


def test_builtin_llm_reranking_still_uses_priced_bounded_dispatch(
    boundary: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.llm import client as llm_client
    from server.llm import customer, openai
    from server.memory.app import rerank
    from server.observability import provider_budget

    async def check() -> None:
        sent = []

        async def transport(request: httpx.Request) -> httpx.Response:
            assert budget.reserved > 0
            sent.append(request)
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": '{"ranked": [1, 0]}'}],
                        }
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 5},
                },
            )

        async def who() -> Any:
            return object()

        async def headers(_: str) -> dict[str, str]:
            return {}

        model = openai.OpenAILLMClient("gpt-4o-mini", api_key="fixture-key")
        monkeypatch.setattr(model, "_headers", headers)
        monkeypatch.setattr(llm_client, "get_llm_client", lambda: model)
        monkeypatch.setattr(customer, "require", who)
        monkeypatch.setattr(customer, "headers", lambda _: {})
        monkeypatch.setattr(rerank, "reads_llm_free", lambda: False)
        first = rerank.RerankCandidate("one", "ns", "one", "fact", 0.8)
        second = rerank.RerankCandidate("two", "ns", "two", "fact", 0.7)
        async with _http._build(
            transport=httpx.MockTransport(transport), event_hooks={"request": [_http._on_request]}
        ) as client:
            monkeypatch.setattr(openai, "_http_client", lambda: client)
            with provider_budget.limited(1) as budget:
                ordered = await rerank.rerank(
                    endpoint="builtin:openai",
                    prompt_template="",
                    query="query",
                    candidates=[first, second],
                )
        assert ordered == [second, first]
        assert len(sent) == 1 and sent[0].url.path.endswith("/responses")
        payload = json.loads(sent[0].content)
        assert payload["store"] is False
        assert payload["max_output_tokens"] > 0
        assert len(budget.sends) == 1 and budget.sends[0].reported

    asyncio.run(check())


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("embeddings", {"model": "fake-ngram-3072", "input": "query"}),
        ("chat/completions", {"model": "fake-echo-1", "messages": [], "max_tokens": 100}),
    ],
)
def test_offline_fake_prices_cannot_authorize_http_inference(path: str, payload: dict) -> None:
    from server.observability import provider_budget

    with pytest.raises(AssistantUpstreamError):
        with provider_budget.limited(1) as budget:
            provider_budget.sending(
                httpx.Request("POST", "https://provider.invalid/" + path, json=payload)
            )
    assert budget.sends == []


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "chat/completions",
            {
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://content.invalid/image"},
                            }
                        ],
                    }
                ],
                "max_tokens": 10,
            },
        ),
        (
            "chat/completions",
            {
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": "YQ==", "format": "wav"},
                            }
                        ],
                    }
                ],
                "max_tokens": 10,
            },
        ),
        (
            "v1/messages",
            {
                "model": "claude-sonnet-4-6",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "url",
                                    "url": "https://content.invalid/document",
                                },
                            }
                        ],
                    }
                ],
                "max_tokens": 10,
            },
        ),
        (
            "v1/messages",
            {
                "model": "claude-sonnet-4-6",
                "messages": [],
                "system": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="},
                    }
                ],
                "max_tokens": 10,
            },
        ),
        (
            "chat/completions",
            {
                "model": "gpt-4o-mini",
                "messages": [],
                "tools": [{"type": "web_search"}],
                "max_tokens": 10,
            },
        ),
    ],
)
def test_nontext_or_external_work_cannot_use_the_text_token_bound(path: str, payload: dict) -> None:
    from server.observability import provider_budget

    with pytest.raises(AssistantUpstreamError):
        with provider_budget.limited(1) as budget:
            provider_budget.sending(
                httpx.Request("POST", "https://provider.invalid/" + path, json=payload)
            )
    assert budget.sends == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"store": True},
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        {"previous_response_id": "saved-response"},
        {"tools": [{"type": "web_search"}]},
        {"input": [{"role": "user", "content": [{"type": "input_image"}]}]},
    ],
)
def test_responses_refuses_work_without_a_text_and_output_bound(overrides: dict) -> None:
    from server.observability import provider_budget

    payload = {
        "model": "gpt-4o-mini",
        "input": [{"role": "user", "content": "query"}],
        "store": False,
        "max_output_tokens": 16,
    } | overrides
    with pytest.raises(AssistantUpstreamError):
        with provider_budget.limited(1) as budget:
            provider_budget.sending(
                httpx.Request("POST", "https://provider.invalid/responses", json=payload)
            )
    assert budget.sends == []
