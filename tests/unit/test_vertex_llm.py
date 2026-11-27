"""Native Vertex requests and credential refresh without a Google account."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import nullcontext
from typing import Any
from unittest.mock import Mock

import google.auth
import httpx
import pytest
from google.auth import transport
from google.auth.exceptions import DefaultCredentialsError

from server.clients._http import _build, _on_request
from server.core.errors import AssistantUpstreamError, GatewayRequiredError
from server.llm import customer, traits, vertex, vertex_auth
from server.memory.domain.conflict_resolution import RESPONSE_FORMAT
from server.observability import cost_accumulator
from server.settings import settings

_MODEL = "gemini-3.5-flash-lite"
_PROJECT = "vertex-test-project"


class Credentials:
    def __init__(self) -> None:
        self.token = ""
        self.refreshes = 0

    def before_request(self, request: Any, method: str, url: str, headers: dict[str, str]) -> None:
        if not self.token:
            self.refreshes += 1
            self.token = f"test-token-{self.refreshes}"
        headers.update(authorization=f"Bearer {self.token}")
        headers["x-goog-user-project"] = "vertex-quota-project"


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> Credentials:
    adc = Credentials()
    monkeypatch.setattr(google.auth, "default", Mock(return_value=(adc, "local-adc-project")))
    monkeypatch.setattr(vertex_auth, "_credentials", None)
    monkeypatch.setattr(vertex_auth, "_lock", asyncio.Lock())
    monkeypatch.setattr(traits, "_TRAITS", {})
    monkeypatch.setattr(settings, "vertex_project", _PROJECT)
    monkeypatch.setattr(settings, "vertex_location", "us-central1")
    monkeypatch.setattr(settings, "llm_retry_max_wait_s", 0)
    return adc


@pytest.fixture
async def model_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[list[httpx.Request], list[httpx.Response]]]:
    sent: list[httpx.Request] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return responses.pop(0)

    async with _build(
        transport=httpx.MockTransport(handler), event_hooks={"request": [_on_request]}
    ) as wire:
        monkeypatch.setattr(vertex, "get_model_client", lambda: wire)
        with customer.system():
            yield sent, responses


def _completion(text: str = "answer", *, stop: str = "STOP") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "finishReason": stop,
                    "content": {"parts": [{"text": "reasoning", "thought": True}, {"text": text}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 60,
                "cachedContentTokenCount": 20,
                "candidatesTokenCount": 8,
                "thoughtsTokenCount": 3,
            },
        },
    )


async def test_native_json_completion_routes_to_vertex_and_accounts_reported_usage(
    credentials: Credentials, model_responses: tuple[list[httpx.Request], list[httpx.Response]]
) -> None:
    sent, responses = model_responses
    responses.append(_completion('{"answer": true}'))
    client = vertex.VertexLLMClient(f"vertex:{_MODEL}")
    with cost_accumulator.isolated_op() as usage:
        result = await client.complete("Extract facts", "my note", 50, {"type": "json_object"}, 0.3)
    (request,) = sent
    assert str(request.url) == (
        f"https://us-central1-aiplatform.googleapis.com/v1/projects/{_PROJECT}"
        f"/locations/us-central1/publishers/google/models/{_MODEL}:generateContent"
    )
    assert request.headers["authorization"] == "Bearer test-token-1"
    assert request.headers["x-goog-user-project"] == "vertex-quota-project"
    assert json.loads(request.content) == {
        "systemInstruction": {"parts": [{"text": "Extract facts"}]},
        "contents": [{"role": "user", "parts": [{"text": "my note"}]}],
        "generationConfig": {
            "maxOutputTokens": 50,
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
    }
    assert (result.provider, result.model, result.stop_reason) == ("vertex", client.model, "stop")
    assert result.model == f"vertex:{_MODEL}"
    assert (result.text, result.tokens_in, result.tokens_out) == ('{"answer": true}', 60, 11)
    assert result.cached_input_tokens == 20
    assert (usage.llm_calls, usage.llm_input_tokens, usage.llm_output_tokens) == (1, 60, 11)
    assert usage.llm_cache_read_tokens == 20
    assert usage.cost_usd > 0
    assert credentials.refreshes == 1


async def test_vertex_sends_the_conflict_resolver_schema_to_google(
    credentials: Credentials, model_responses: tuple[list[httpx.Request], list[httpx.Response]]
) -> None:
    sent, responses = model_responses
    responses.append(_completion('{"peers": []}'))
    result = await vertex.VertexLLMClient(_MODEL).complete(
        "Resolve the conflict", "Two notes", response_format=RESPONSE_FORMAT
    )
    (request,) = sent
    config = json.loads(request.content)["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == RESPONSE_FORMAT["json_schema"]["schema"]
    assert json.loads(result.text) == {"peers": []}


@pytest.mark.parametrize(
    "response_format",
    [
        {"type": "csv"},
        {"type": "json_schema"},
        {"type": "json_schema", "json_schema": {"schema": []}},
        {
            "type": "json_schema",
            "json_schema": {"schema": {"properties": {"peer": {"pattern": ".*"}}}},
        },
    ],
)
async def test_vertex_refuses_unsupported_response_formats_before_dispatch(
    credentials: Credentials,
    model_responses: tuple[list[httpx.Request], list[httpx.Response]],
    response_format: dict[str, Any],
) -> None:
    sent, _ = model_responses
    with pytest.raises(ValueError, match="Vertex AI"):
        await vertex.VertexLLMClient(_MODEL).complete("", "note", response_format=response_format)
    assert not sent
    assert credentials.refreshes == 0


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {"promptTokenCount": 60},
        {"candidatesTokenCount": 8},
        {"promptTokenCount": True, "candidatesTokenCount": 8},
        {"promptTokenCount": 60, "candidatesTokenCount": -8},
        {"promptTokenCount": 60, "candidatesTokenCount": 8, "thoughtsTokenCount": "unknown"},
        {"promptTokenCount": 60, "candidatesTokenCount": 8, "cachedContentTokenCount": 80},
    ],
)
async def test_missing_or_malformed_usage_preserves_the_answer_and_marks_cost_estimated(
    credentials: Credentials,
    model_responses: tuple[list[httpx.Request], list[httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, Any] | None,
) -> None:
    _, responses = model_responses
    answer = _completion().json()
    answer.pop("usageMetadata")
    if metadata is not None:
        answer["usageMetadata"] = metadata
    responses.append(httpx.Response(200, json=answer))
    span = Mock()
    monkeypatch.setattr(vertex, "generation_span", Mock(return_value=nullcontext(span)))

    with cost_accumulator.isolated_op() as usage:
        result = await vertex.VertexLLMClient(_MODEL).complete("", "note")
    assert (result.text, result.stop_reason, result.tokens_in, result.tokens_out) == (
        "answer",
        "stop",
        0,
        0,
    )
    assert usage.llm_calls == 1
    assert usage.cost_estimated and usage.cost_usd > 0
    span.record_usage.assert_not_called()
    span.record_accounting.assert_called_once_with(
        usage_status="unknown", price_model=result.model, estimated_cost=usage.cost_usd
    )


async def test_global_endpoint_refreshes_adc_again_when_the_token_expires(
    credentials: Credentials,
    model_responses: tuple[list[httpx.Request], list[httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, responses = model_responses
    responses.extend([_completion(), _completion(), _completion()])
    monkeypatch.setattr(settings, "vertex_location", "global")
    client = vertex.VertexLLMClient(_MODEL)
    await asyncio.gather(client.complete("", "one"), client.complete("", "two"))
    credentials.token = ""
    await client.complete("", "three")
    assert all(r.url.host == "aiplatform.googleapis.com" for r in sent)
    assert all("/locations/global/" in r.url.path for r in sent)
    assert [r.headers["authorization"] for r in sent] == [
        "Bearer test-token-1",
        "Bearer test-token-1",
        "Bearer test-token-2",
    ]
    google.auth.default.assert_called_once()
    assert google.auth.default.call_args.kwargs["scopes"] == [
        "https://www.googleapis.com/auth/cloud-platform"
    ]


async def test_cancelled_credential_refresh_never_sends_a_model_request(
    credentials: Credentials,
    model_responses: tuple[list[httpx.Request], list[httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, responses = model_responses
    responses.append(_completion())
    released = threading.Event()
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    before_request: Callable[..., None] = credentials.before_request

    def blocked(*args: Any) -> None:
        loop.call_soon_threadsafe(started.set)
        if not released.wait(timeout=2):
            raise TimeoutError("test never released refresh")
        before_request(*args)

    monkeypatch.setattr(credentials, "before_request", blocked)
    client = vertex.VertexLLMClient(_MODEL)
    first = asyncio.create_task(client.complete("", "cancelled"))
    await started.wait()
    first.cancel()
    second = asyncio.create_task(client.complete("", "kept"))
    try:
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0)
        assert not second.done()
        assert not sent
    finally:
        released.set()
    assert (await second).text == "answer"
    (request,) = sent
    assert json.loads(request.content)["contents"][0]["parts"][0]["text"] == "kept"
    assert credentials.refreshes == 1


async def test_missing_adc_fails_before_any_model_request(
    credentials: Credentials,
    model_responses: tuple[list[httpx.Request], list[httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, _ = model_responses
    monkeypatch.setattr(
        google.auth, "default", Mock(side_effect=DefaultCredentialsError("private"))
    )
    with pytest.raises(RuntimeError, match="Application Default Credentials") as failure:
        await vertex.VertexLLMClient(_MODEL).complete("", "my note")
    assert "private" not in str(failure.value)
    assert not sent


async def test_adc_network_call_obeys_the_completion_deadline(
    credentials: Credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.auth.transport import requests as google_requests

    underlying = Mock()
    monkeypatch.setattr(google_requests, "Request", Mock(return_value=underlying))

    def refresh(request: transport.Request, method: str, url: str, headers: dict[str, str]) -> None:
        request("https://identity.example/token", method="POST", timeout=120)
        headers["authorization"] = "Bearer test-token-1"

    monkeypatch.setattr(credentials, "before_request", refresh)
    headers = await vertex_auth.headers("https://aiplatform.googleapis.com", 1.0)
    assert headers == {"authorization": "Bearer test-token-1"}
    timeout = underlying.call_args.kwargs["timeout"]
    assert 0 < timeout <= 1.0


async def test_vertex_cannot_bypass_gateway_or_an_assistant_tool_budget(
    credentials: Credentials,
    model_responses: tuple[list[httpx.Request], list[httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import provider_budget

    sent, _ = model_responses
    client = vertex.VertexLLMClient(_MODEL)
    monkeypatch.setattr(settings, "inference_gateway_required", True)
    with pytest.raises(GatewayRequiredError):
        await client.complete("", "my note")
    assert credentials.refreshes == 0

    monkeypatch.setattr(settings, "inference_gateway_required", False)
    with pytest.raises(AssistantUpstreamError), provider_budget.limited(1):
        await client.complete("", "my note")
    assert not sent


async def test_vertex_retries_quota_failures_and_never_follows_model_redirects(
    credentials: Credentials, model_responses: tuple[list[httpx.Request], list[httpx.Response]]
) -> None:
    sent, responses = model_responses
    responses.extend(
        [httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}), _completion()]
    )
    client = vertex.VertexLLMClient(_MODEL)
    assert (await client.complete("", "my note")).text == "answer"
    assert len(sent) == 2

    responses.append(httpx.Response(307, headers={"location": "https://other.example/transfer"}))
    with pytest.raises(httpx.HTTPStatusError) as failure:
        await client.complete("", "my note")
    assert failure.value.response.status_code == 307
    assert len(sent) == 3
    assert credentials.refreshes == 1


async def test_vertex_does_not_retry_iam_refusal_or_expose_its_message(
    credentials: Credentials, model_responses: tuple[list[httpx.Request], list[httpx.Response]]
) -> None:
    sent, responses = model_responses
    responses.append(
        httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "private"}})
    )
    with pytest.raises(httpx.HTTPStatusError, match="PERMISSION_DENIED") as failure:
        await vertex.VertexLLMClient(_MODEL).complete("", "my note")
    assert "private" not in str(failure.value)
    assert "private" not in failure.value.response.text
    assert len(sent) == 1


async def test_vertex_refuses_an_oversized_response_before_decoding(
    credentials: Credentials,
    model_responses: tuple[list[httpx.Request], list[httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, responses = model_responses
    monkeypatch.setattr(vertex, "_MAX_RESPONSE_BYTES", 32)
    responses.append(httpx.Response(200, content=b"x" * 33))

    with pytest.raises(ValueError, match="response exceeds the model response limit"):
        await vertex.VertexLLMClient(_MODEL).complete("", "my note")
    assert len(sent) == 1


async def test_token_limit_and_prompt_safety_survive_translation(
    credentials: Credentials, model_responses: tuple[list[httpx.Request], list[httpx.Response]]
) -> None:
    sent, responses = model_responses
    responses.extend(
        [
            _completion("partial", stop="MAX_TOKENS"),
            httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}),
        ]
    )
    client = vertex.VertexLLMClient(_MODEL)
    limited = await client.complete("", "cap", 20)
    with cost_accumulator.isolated_op() as usage:
        blocked = await client.complete("", "blocked")
    assert (limited.text, limited.stop_reason) == ("partial", "length")
    assert (blocked.text, blocked.stop_reason) == ("", "content_filter")
    assert len(sent) == 2
    assert usage.llm_calls == 1
    assert usage.cost_estimated and usage.cost_usd > 0


async def test_empty_token_limited_reply_counts_both_completions_before_returning(
    credentials: Credentials, model_responses: tuple[list[httpx.Request], list[httpx.Response]]
) -> None:
    sent, responses = model_responses
    responses.extend([_completion("", stop="MAX_TOKENS"), _completion()])
    client = vertex.VertexLLMClient(_MODEL)
    with cost_accumulator.isolated_op() as usage:
        result = await client.complete("", "my note", 20)
    first, second = (
        json.loads(request.content)["generationConfig"]["maxOutputTokens"] for request in sent
    )
    assert second > first == 20
    assert result.text == "answer"
    assert usage.llm_calls == 2
    assert usage.llm_output_tokens == 22


@pytest.mark.parametrize("model", ["https://other.example/model", "gemini-foo/../bar", ""])
def test_model_id_cannot_replace_the_publisher_path(credentials: Credentials, model: str) -> None:
    with pytest.raises(ValueError, match="Gemini model ID"):
        vertex.VertexLLMClient(model)
