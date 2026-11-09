"""SDK assistant client: `agent.assistant.stream` reads the chat route's server-sent
events into text pieces and one answer with its checked citations, maps a refusal and
a mid-stream error event to the typed errors, never resends, and `SyncAgent` iterates
it. CH-free: a MockTransport stands in for Loom."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from loom import AssistantAnswer, AssistantCitation, AssistantText
from loom.agent import Agent
from loom.errors import AuthInsufficient, BackendUnavailable, LoomError, RateLimited
from loom.sync import SyncAgent

CITED = [
    {"kind": "memory", "text": "[memory:a/b/c@v3]", "verified": True, "reason": "", "ref": "a/b/c"}
]


def _sse(*events: Any) -> bytes:
    lines = [f"data: {json.dumps(e)}\n\n" for e in events]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def _chunk(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": text}}],
        **extra,
    }


ANSWER = _sse(
    _chunk("Deploys freeze "),
    _chunk("on Fridays."),
    {"id": "chatcmpl-1", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
    {"object": "loom.citations", "id": "chatcmpl-1", "citations": CITED},
)


def _agent(handler: Any) -> Agent:
    a = Agent(org="acme", namespace="handbook", agent="bot", api_key="k", base_url="http://x")
    a.transport._client = httpx.AsyncClient(
        base_url="http://x",
        headers={"Authorization": "Bearer k"},
        transport=httpx.MockTransport(handler),
    )
    return a


def _collect(a: Agent, **kw: Any) -> list[Any]:
    async def run() -> list[Any]:
        return [e async for e in a.assistant.stream("When are deploys frozen?", **kw)]

    return asyncio.run(run())


def test_the_stream_yields_text_then_the_answer_with_its_citations() -> None:
    sent: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        sent.append(r)
        return httpx.Response(200, content=ANSWER, headers={"content-type": "text/event-stream"})

    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."}]
    events = _collect(_agent(handler), history=history)
    assert events[:2] == [AssistantText("Deploys freeze "), AssistantText("on Fridays.")]
    answer = events[-1]
    assert isinstance(answer, AssistantAnswer) and len(events) == 3
    assert answer.text == "Deploys freeze on Fridays." and answer.run_id == "chatcmpl-1"
    assert (answer.model, answer.prompt_tokens, answer.completion_tokens) == ("m", 9, 4)
    assert answer.verified == (AssistantCitation("memory", "[memory:a/b/c@v3]", True, ref="a/b/c"),)
    (r,) = sent
    assert r.url.path == "/v1/assistant/chat/completions"
    assert r.headers["X-Loom-Namespace"] == "handbook"
    assert r.headers["Authorization"] == "Bearer k"
    body = json.loads(r.content)
    assert body == {
        "stream": True,
        "messages": [*history, {"role": "user", "content": "When are deploys frozen?"}],
    }


def test_a_refusal_raises_its_typed_error_and_is_not_resent() -> None:
    sent: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        sent.append(r)
        detail = {"code": "RATE_LIMITED", "message": "the assistant's share is spent"}
        return httpx.Response(429, json={"detail": detail}, headers={"Retry-After": "60"})

    with pytest.raises(RateLimited):
        _collect(_agent(handler))
    assert len(sent) == 1  # an answer runs once: even a 429 is not paced and resent


def test_another_namespace_is_named_per_call() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        assert r.headers["X-Loom-Namespace"] == "library"
        detail = {"code": "AUTH_INSUFFICIENT", "message": "no viewer"}
        return httpx.Response(403, json={"detail": detail})

    with pytest.raises(AuthInsufficient):
        _collect(_agent(handler), namespace="library")


def test_an_error_event_after_the_text_raises_the_typed_error() -> None:
    error = {"code": "ASSISTANT_UPSTREAM_FAILED", "message": "LibreChat failed", "status": 503}
    body = _sse(_chunk("Deploys "), {"error": error})
    seen: list[Any] = []

    async def run() -> None:
        a = _agent(lambda r: httpx.Response(200, content=body))
        async for e in a.assistant.stream("q"):
            seen.append(e)

    with pytest.raises(BackendUnavailable) as caught:
        asyncio.run(run())
    assert caught.value.code == "ASSISTANT_UPSTREAM_FAILED"
    assert seen == [AssistantText("Deploys ")]


@pytest.mark.parametrize("status", [None, "busy", 0])
def test_an_error_event_without_a_readable_status_raises_as_a_503(status: Any) -> None:
    error = {"code": "ASSISTANT_UPSTREAM_FAILED", "message": "LibreChat failed", "status": status}
    body = _sse(_chunk("Deploys "), {"error": error})
    with pytest.raises(BackendUnavailable) as caught:
        _collect(_agent(lambda r: httpx.Response(200, content=body)))
    assert caught.value.status == 503


def test_an_unreadable_chunk_is_skipped_and_unreadable_usage_reads_as_0() -> None:
    body = (
        _sse(_chunk("Deploys "))[: -len(b"data: [DONE]\n\n")]
        + b"data: not json\n\n"
        + _sse(
            _chunk("freeze."),
            {
                "id": "chatcmpl-1",
                "choices": [],
                "usage": {"prompt_tokens": "many", "completion_tokens": float("inf")},
            },
        )
    )
    events = _collect(_agent(lambda r: httpx.Response(200, content=body)))
    answer = events[-1]
    assert isinstance(answer, AssistantAnswer)
    assert (answer.text, answer.prompt_tokens, answer.completion_tokens) == (
        "Deploys freeze.",
        0,
        0,
    )


def test_a_stream_that_ends_before_the_answer_began_is_an_error() -> None:
    with pytest.raises(LoomError, match="before the answer began"):
        _collect(_agent(lambda r: httpx.Response(200, content=b"data: [DONE]\n\n")))


def test_a_connection_cut_mid_stream_is_unreachable() -> None:
    class _Cut(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b'data: {"id": "chatcmpl-1", "choices": []}\n\n'
            raise httpx.ReadError("connection reset")

    with pytest.raises(BackendUnavailable) as caught:
        _collect(_agent(lambda r: httpx.Response(200, stream=_Cut())))
    assert caught.value.code == "NETWORK"


def test_each_read_waits_read_timeout_s() -> None:
    waits: list[float] = []

    def handler(r: httpx.Request) -> httpx.Response:
        waits.append(r.extensions["timeout"]["read"])
        return httpx.Response(200, content=ANSWER)

    _collect(_agent(handler))
    _collect(_agent(handler), read_timeout_s=5.0)
    assert waits == [300.0, 5.0]


def test_ask_returns_the_whole_answer() -> None:
    a = _agent(lambda r: httpx.Response(200, content=ANSWER))
    answer = asyncio.run(a.assistant.ask("q"))
    assert answer.text == "Deploys freeze on Fridays." and answer.citations[0].verified


def test_sync_agent_iterates_the_stream() -> None:
    with SyncAgent(
        org="acme", namespace="handbook", agent="bot", api_key="k", base_url="http://x"
    ) as sa:
        sa.transport._client = httpx.AsyncClient(
            base_url="http://x",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=ANSWER)),
        )
        events = list(sa.assistant.stream("q"))
        assert isinstance(events[-1], AssistantAnswer) and len(events) == 3
        assert sa.assistant.ask("q").run_id == "chatcmpl-1"
