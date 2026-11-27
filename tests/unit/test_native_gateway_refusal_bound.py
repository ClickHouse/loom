"""Native streaming refusals pass through the real shared model-client hooks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from server.clients import _http
from server.core.errors import ModelCallDeferred, ModelCallRefused
from server.llm import agent_http, gateway
from server.llm.agent import Turn
from server.settings import Settings, settings


class RefusalBody(httpx.AsyncByteStream):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.chunks = 0
        self.closed = False
        self.cancelled = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.kind == "stalled":
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        if self.kind == "success":
            self.chunks += 1
            answer = {
                "choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            yield b"data: " + json.dumps(answer).encode() + b"\n\ndata: [DONE]\n\n"
        elif self.kind == "oversized":
            for _ in range(8):
                self.chunks += 1
                yield b"x" * 65536
        else:
            self.chunks += 1
            code = {
                "identity": "agent_denied",
                "wait": "agent_request_limit",
            }.get(self.kind, "other_refusal")
            yield json.dumps({"error": {"code": code}}).encode()

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def gateway_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Agent": "{agent}"}')
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "inference_customer_header", "")
    monkeypatch.setattr(settings, "llm_request_timeout_s", 0.02)
    monkeypatch.setattr(
        settings,
        "inference_identity_refusals_json",
        Settings.model_fields["inference_identity_refusals_json"].default,
    )
    gateway._reset_for_tests()
    yield
    gateway._reset_for_tests()


async def native_response(monkeypatch: pytest.MonkeyPatch, body: RefusalBody) -> list[str]:
    monkeypatch.setattr(_http, "_model_client", None)
    client = _http.get_model_client()
    monkeypatch.setattr(
        client,
        "_transport",
        httpx.MockTransport(
            lambda _: httpx.Response(
                200 if body.kind == "success" else 429 if body.kind == "wait" else 403,
                stream=body,
                headers={"Retry-After": "1"},
            )
        ),
    )
    monkeypatch.setattr(agent_http, "get_model_client", _http.get_model_client)

    async def request(*_: object) -> tuple[str, dict, dict]:
        return "https://gateway.example/v1/chat/completions", {"X-Agent": "test"}, {"model": "m"}

    monkeypatch.setattr(agent_http.HTTPAgentModel, "_request", request)
    try:
        return [
            text
            async for text in agent_http.HTTPAgentModel("openai", "m").stream([], [], 1, Turn())
        ]
    finally:
        await client.aclose()


@pytest.mark.parametrize("kind", ["oversized", "stalled"])
def test_native_refusal_read_is_bounded_before_sse(
    gateway_settings: None, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    body = RefusalBody(kind)

    async def scenario() -> None:
        expected = httpx.HTTPStatusError if kind == "oversized" else httpx.ReadTimeout
        with pytest.raises(expected) as error:
            async with asyncio.timeout(0.5):
                await native_response(monkeypatch, body)
        assert "xxxx" not in str(error.value)

    asyncio.run(scenario())
    assert body.closed
    assert body.chunks <= 5
    assert body.cancelled == (kind == "stalled")


@pytest.mark.parametrize("kind", ["identity", "unmatched"])
def test_native_refusal_retains_exact_identity_classification(
    gateway_settings: None, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    body = RefusalBody(kind)
    expected = ModelCallRefused if kind == "identity" else httpx.HTTPStatusError
    with pytest.raises(expected):
        asyncio.run(native_response(monkeypatch, body))
    assert body.closed and body.chunks == 1
    assert bool(gateway._held) == (kind == "identity")


def test_native_success_keeps_its_stream_and_no_identity_hold(
    gateway_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = RefusalBody("success")
    assert asyncio.run(native_response(monkeypatch, body)) == ["answer"]
    assert body.closed and body.chunks == 1
    assert not gateway._held


def test_native_short_identity_wait_is_never_replayed(
    gateway_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = RefusalBody("wait")
    with pytest.raises(ModelCallDeferred) as deferred:
        asyncio.run(native_response(monkeypatch, body))
    assert deferred.value.to_detail()["retry_after_s"] == 1
    assert body.closed and body.chunks == 1
    assert gateway._held
