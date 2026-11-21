"""Tool-owned provider sends spend an answer's allowance before dispatch."""

from __future__ import annotations

import asyncio
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
            "model/claude-sonnet-4-6/converse",
            {"messages": [], "inferenceConfig": {"maxTokens": 12}},
        ),
        ("model/text-embedding-3-small/invoke", {"texts": ["query"]}),
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
