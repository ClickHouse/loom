"""Native Vertex assistant streaming with a local HTTP transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from server.core.errors import AssistantUpstreamError, GatewayRequiredError
from server.llm import agent_vertex, customer, vertex_auth
from server.llm.agent import Turn, Usage, model_for
from server.settings import settings

_MODEL = "gemini-3.1-flash-lite"
_PROJECT = "vertex-test-project"
_MODEL_URL = (
    f"https://aiplatform.googleapis.com/v1/projects/{_PROJECT}/locations/global"
    f"/publishers/google/models/{_MODEL}:streamGenerateContent"
)
_MESSAGES = [
    {"role": "system", "content": "Answer the question."},
    {"role": "user", "content": "What did I write?"},
]


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body[:17]
        yield self.body[17:]

    async def aclose(self) -> None:
        self.closed = True


def _sse(*frames: dict[str, Any]) -> bytes:
    return "".join("data: " + json.dumps(frame) + "\r\n\r\n" for frame in frames).encode()


@pytest.fixture
def adc(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(settings, "vertex_project", _PROJECT)
    monkeypatch.setattr(settings, "vertex_location", "global")
    authorize = AsyncMock(
        return_value={"authorization": "Bearer fixture-token", "x-goog-user-project": _PROJECT}
    )
    monkeypatch.setattr(vertex_auth, "headers", authorize)
    return authorize


@pytest.fixture
async def model_wire(
    monkeypatch: pytest.MonkeyPatch,
    adc: AsyncMock,
) -> AsyncIterator[tuple[list[httpx.Request], list[httpx.Response]]]:
    sent: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def send(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return replies.pop(0)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(send), follow_redirects=True
    ) as client:
        monkeypatch.setattr(agent_vertex, "get_model_client", lambda: client)
        with customer.system():
            yield sent, replies


async def test_vertex_assistant_streams_text_and_accounts_final_usage(
    model_wire: tuple[list[httpx.Request], list[httpx.Response]],
    adc: AsyncMock,
) -> None:
    sent, replies = model_wire
    stop = {
        "candidates": [
            {
                "content": {"parts": [{"text": "", "thoughtSignature": "c2lnbmF0dXJl"}]},
                "finishReason": "STOP",
            }
        ]
    }
    usage = {
        "usageMetadata": {
            "promptTokenCount": 50,
            "cachedContentTokenCount": 12,
            "candidatesTokenCount": 4,
            "thoughtsTokenCount": 3,
        }
    }
    body = ResponseStream(
        _sse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Reasoning summary", "thought": True},
                                {"text": "Your"},
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 44, "candidatesTokenCount": 1},
            },
            {"candidates": [{"content": {"parts": [{"text": " note."}]}}]},
            stop,
            usage,
        )
    )
    replies.append(httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body))
    turn = Turn()

    assert [text async for text in model_for("vertex", _MODEL).stream(_MESSAGES, [], 20, turn)] == [
        "Your",
        " note.",
    ]
    assert turn.text == "Your note."
    assert turn.stop == "stop"
    assert turn.reported_usage() == Usage(input=38, output=7, cache_read=12, reasoning=3)
    assert body.closed

    (request,) = sent
    assert request.method == "POST"
    assert str(request.url) == f"{_MODEL_URL}?alt=sse"
    assert request.headers["authorization"] == "Bearer fixture-token"
    assert request.headers["x-goog-user-project"] == _PROJECT
    assert request.headers["accept"] == "text/event-stream"
    adc.assert_awaited_once_with(_MODEL_URL, settings.llm_request_timeout_s)
    assert json.loads(request.content) == {
        "systemInstruction": {"parts": [{"text": "Answer the question."}]},
        "contents": [{"role": "user", "parts": [{"text": "What did I write?"}]}],
        "generationConfig": {"maxOutputTokens": 20},
    }


@pytest.mark.parametrize(
    ("wire_body", "error"),
    [
        pytest.param(
            _sse(
                {
                    "promptFeedback": {"blockReason": "SAFETY"},
                    "usageMetadata": {
                        "promptTokenCount": 50,
                    },
                }
            ),
            "refused or cut",
            id="blocked-prompt",
        ),
        pytest.param(
            _sse(
                {
                    "candidates": [{"finishReason": "MAX_TOKENS"}],
                    "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 20},
                }
            ),
            "refused or cut",
            id="truncated-candidate",
        ),
        pytest.param(
            _sse(
                {
                    "candidates": [{"content": {"parts": [{"text": "Partial answer."}]}}],
                    "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 1},
                }
            ),
            "without a finish reason",
            id="closed-before-stop",
        ),
        pytest.param(
            _sse(
                {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "Answer."}]},
                            "finishReason": "STOP",
                        }
                    ],
                }
            ),
            "without complete usage",
            id="missing-usage",
        ),
        pytest.param(
            _sse(
                {
                    "candidates": [{"content": {"parts": [{"text": "Start"}]}}],
                    "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 1},
                },
                {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": " of the answer."}]},
                            "finishReason": "STOP",
                        }
                    ]
                },
            ),
            "without complete usage",
            id="interim-usage-only",
        ),
        pytest.param(
            _sse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": " "},
                                    {"functionCall": {"name": "loom_memory_get", "args": {}}},
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 50},
                }
            ),
            "without complete usage",
            id="tool-and-unaccounted-text",
        ),
        pytest.param(
            _sse(
                {
                    "candidates": [{"finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 50, "cachedContentTokenCount": 51},
                }
            ),
            "invalid cache usage",
            id="invalid-usage",
        ),
        pytest.param(b"data: {bad json}\n\n", "malformed stream frame", id="invalid-sse"),
    ],
)
async def test_vertex_assistant_rejects_unsuccessful_streams(
    model_wire: tuple[list[httpx.Request], list[httpx.Response]],
    wire_body: bytes,
    error: str,
) -> None:
    sent, replies = model_wire
    body = ResponseStream(wire_body)
    replies.append(httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body))

    with pytest.raises(AssistantUpstreamError, match=error):
        _ = [text async for text in model_for("vertex", _MODEL).stream(_MESSAGES, [], 20, Turn())]
    assert len(sent) == 1
    assert body.closed


@pytest.mark.parametrize(
    ("setting", "value"),
    [("inference_gateway_required", True), ("inference_token_url", "https://gateway.invalid")],
)
async def test_vertex_assistant_refuses_gateway_bypass_before_adc(
    model_wire: tuple[list[httpx.Request], list[httpx.Response]],
    adc: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: bool | str,
) -> None:
    sent, _ = model_wire
    monkeypatch.setattr(settings, setting, value)
    turn = Turn()

    with pytest.raises(GatewayRequiredError):
        _ = [text async for text in model_for("vertex", _MODEL).stream(_MESSAGES, [], 20, turn)]
    adc.assert_not_awaited()
    assert not turn.sent
    assert not sent


async def test_vertex_assistant_closes_redirect_without_following_it(
    model_wire: tuple[list[httpx.Request], list[httpx.Response]],
) -> None:
    sent, replies = model_wire
    body = ResponseStream(b"")
    replies.append(httpx.Response(307, headers={"location": "https://other.invalid"}, stream=body))

    with pytest.raises(httpx.HTTPStatusError) as failure:
        _ = [text async for text in model_for("vertex", _MODEL).stream(_MESSAGES, [], 20, Turn())]
    assert failure.value.response.status_code == 307
    assert len(sent) == 1
    assert body.closed


async def test_vertex_assistant_reports_unavailable_adc_without_sending(
    model_wire: tuple[list[httpx.Request], list[httpx.Response]],
    adc: AsyncMock,
) -> None:
    sent, _ = model_wire
    adc.side_effect = vertex_auth.VertexCredentialsError("private credential provider response")
    turn = Turn()

    with pytest.raises(AssistantUpstreamError) as failure:
        _ = [text async for text in model_for("vertex", _MODEL).stream(_MESSAGES, [], 20, turn)]
    assert failure.value.to_detail() == {
        "code": "ASSISTANT_UPSTREAM_FAILED",
        "message": "Vertex AI credentials are unavailable. "
        "Check Google Application Default Credentials and IAM.",
        "retry_after_s": 5,
    }
    assert failure.value.__cause__ is None
    assert not turn.sent
    assert not sent
