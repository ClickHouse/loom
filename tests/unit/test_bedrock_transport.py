"""Native signing, retry, privacy and cancellation contracts without AWS calls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from botocore.credentials import ReadOnlyCredentials

from server.clients import bedrock
from server.llm import customer
from server.settings import settings


def test_signing_reads_refreshable_credentials_for_each_request(monkeypatch):
    span = Mock()
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: span)
    credentials = Mock()
    credentials.get_frozen_credentials.side_effect = [
        ReadOnlyCredentials("example-one", "not-a-real-key", "token-one"),
        ReadOnlyCredentials("example-two", "not-a-real-key", "token-two"),
    ]
    session = SimpleNamespace(get_credentials=lambda: credentials)
    connection = bedrock._Connection(
        session, "https://bedrock-runtime.us-east-1.amazonaws.com", "us-east-1"
    )
    monkeypatch.setattr(bedrock, "_connection", lambda region: connection)
    payload = {
        "modelId": "us.anthropic.example:0",
        "messages": [{"role": "user", "content": [{"text": "hello"}]}],
    }
    first = bedrock._signed("converse", payload, {}, "us-east-1")
    second = bedrock._signed("converse", payload, {}, "us-east-1")
    assert "example-one/" in first[2]["Authorization"]
    assert "example-two/" in second[2]["Authorization"]
    assert first[2]["X-Amz-Security-Token"] == "token-one"
    assert second[2]["X-Amz-Security-Token"] == "token-two"
    assert first[0].endswith("/model/us.anthropic.example%3A0/converse")
    assert "modelId" not in json.loads(first[1])
    assert credentials.get_frozen_credentials.call_count == 2
    span.set_attributes.assert_called_with(
        {
            "cloud.provider": "aws",
            "cloud.region": "us-east-1",
            "aws.bedrock.request.model_id": "us.anthropic.example:0",
        }
    )


def test_private_identity_headers_are_filtered_before_signing(monkeypatch):
    credentials = Mock()
    credentials.get_frozen_credentials.return_value = ReadOnlyCredentials(
        "example", "not-a-real-key", None
    )
    connection = bedrock._Connection(
        SimpleNamespace(get_credentials=lambda: credentials), "https://bedrock.example", "us-east-1"
    )
    monkeypatch.setattr(bedrock, "_connection", lambda region: connection)
    monkeypatch.setattr(settings, "inference_identity_headers_json", '{"X-Identity":"{subject}"}')
    _, _, headers = bedrock._signed(
        "converse", {"modelId": "model"}, {"X-Identity": "private", "X-Customer": "org"}, ""
    )
    assert "X-Identity" not in headers
    assert headers["X-Customer"] == "org"
    assert "x-identity" not in headers["Authorization"].lower()


@pytest.mark.asyncio
async def test_auth_errors_are_sanitized_without_retry(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            403,
            headers={"x-amzn-errortype": "ExpiredTokenException"},
            json={"message": "private credential detail"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    monkeypatch.setattr(
        bedrock, "_signed", lambda *args: ("https://bedrock.example/model/m/converse", b"{}", {})
    )
    async with client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await bedrock.call(
                "converse", {"modelId": "m"}, customer.Customer(), request_timeout_s=1, attempts=3
            )
    assert len(requests) == 1
    assert caught.value.response.json() == {"error": {"code": "ExpiredTokenException"}}
    assert "private" not in str(caught.value)
    assert "private" not in caught.value.response.text


@pytest.mark.asyncio
async def test_retryable_failure_signs_each_attempt(monkeypatch):
    responses = iter([httpx.Response(503), httpx.Response(200, json={"ok": True})])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(responses)))
    sign = Mock(return_value=("https://bedrock.example/model/m/converse", b"{}", {}))
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    monkeypatch.setattr(bedrock, "_signed", sign)
    note = Mock()
    async with client:
        result = await bedrock.call(
            "converse",
            {"modelId": "m"},
            customer.Customer(),
            request_timeout_s=1,
            attempts=2,
            max_wait_s=0,
            on_attempt=note,
        )
    assert result == {"ok": True}
    assert sign.call_count == note.call_count == 2


@pytest.mark.asyncio
async def test_signed_requests_never_follow_redirects(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://another.example/"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True)
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await bedrock._post("https://bedrock.example/", b"{}", {"Authorization": "example"}, 1)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cancellation_closes_the_model_response(monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"{}"

        async def aclose(self):
            closed.set()

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
    )
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    async with client:
        task = asyncio.create_task(bedrock._post("https://bedrock.example/", b"{}", {}, 1))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_response_size_is_bounded(monkeypatch):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 9))
    )
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    monkeypatch.setattr(bedrock, "_MAX_RESPONSE_BYTES", 8)
    async with client:
        with pytest.raises(ValueError, match="response limit"):
            await bedrock._post("https://bedrock.example/", b"{}", {}, 1)
