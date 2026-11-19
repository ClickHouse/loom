"""Native signing, retry, privacy and cancellation contracts without AWS calls."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
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


@pytest.mark.parametrize("region,resolved", [("", "us-east-1"), ("us-west-2", "us-west-2")])
def test_sdk_region_and_endpoint_resolution_disables_endpoint_overrides(
    monkeypatch, region, resolved
):
    session, client = Mock(), Mock()
    client.meta.endpoint_url = f"https://bedrock-runtime.{resolved}.amazonaws.com"
    client.meta.region_name = resolved
    session.client.return_value = client
    monkeypatch.setattr("boto3.session.Session", lambda: session)
    bedrock._connection.cache_clear()
    try:
        connection = bedrock._connection(region)
        assert connection.region == resolved
        assert connection.endpoint == client.meta.endpoint_url
        assert session.client.call_args.kwargs["region_name"] == (region or None)
        assert session.client.call_args.kwargs["config"].ignore_configured_endpoint_urls
        assert set(session.client.call_args.kwargs) == {"region_name", "config"}
        client.close.assert_called_once()
    finally:
        bedrock._connection.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,fields",
    [
        ("converse", {"messages": [{"role": "user", "content": [{"text": "question"}]}]}),
        ("invoke_model", {"body": '{"texts":["é"]}'.encode()}),
    ],
)
async def test_outgoing_profile_arn_request_retains_its_sdk_signature(
    monkeypatch, operation, fields
):
    frozen = ReadOnlyCredentials("example", "not-a-real-key", "example-session")
    credentials = SimpleNamespace(get_frozen_credentials=lambda: frozen)
    connection = bedrock._Connection(
        SimpleNamespace(get_credentials=lambda: credentials),
        "https://bedrock-runtime.us-east-1.amazonaws.com",
        "us-east-1",
    )
    monkeypatch.setattr(bedrock, "_connection", lambda region: connection)
    monkeypatch.setattr(
        "botocore.auth.get_current_datetime", lambda: datetime(2024, 1, 1, tzinfo=UTC)
    )
    requests = []

    def respond(request):
        requests.append(request)
        names = (
            request.headers["Authorization"]
            .split("SignedHeaders=", 1)[1]
            .split(",", 1)[0]
            .split(";")
        )
        expected = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={name: request.headers[name] for name in names},
        )
        SigV4Auth(frozen, "bedrock", "us-east-1").add_auth(expected)
        assert request.headers["Authorization"] == expected.headers["Authorization"]
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    async with client:
        await bedrock.call(
            operation,
            {
                "modelId": "arn:aws:bedrock:us-east-1:000000000000:inference-profile/name%2Fpart:0",
                **fields,
            },
            customer.Customer(),
            request_timeout_s=1,
            attempts=1,
        )
    assert len(requests) == 1
    assert "%2F" in str(requests[0].url)
    if operation == "invoke_model":
        assert requests[0].content == fields["body"]


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


@pytest.mark.asyncio
async def test_deadline_covers_pending_credential_resolution(monkeypatch):
    from server.llm._retry import is_provider_outage

    async def sign(*args):
        await asyncio.sleep(0.1)
        return "https://bedrock.example/", b"{}", {}

    post = AsyncMock(return_value=bedrock._Reply({"ok": True}, {}))
    monkeypatch.setattr(bedrock.asyncio, "to_thread", sign)
    monkeypatch.setattr(bedrock, "_post", post)
    with pytest.raises(TimeoutError) as caught:
        await bedrock.call(
            "converse", {"modelId": "m"}, customer.Customer(), request_timeout_s=0.02, attempts=1
        )
    post.assert_not_awaited()
    assert is_provider_outage(caught.value)


@pytest.mark.asyncio
async def test_deadline_covers_retries_and_backoff(monkeypatch):
    monkeypatch.setattr(
        bedrock.asyncio,
        "to_thread",
        AsyncMock(
            return_value=("https://bedrock.example/", b"{}", {}),
        ),
    )
    post = AsyncMock(
        side_effect=[httpx.ConnectError("unavailable"), bedrock._Reply({"ok": True}, {})]
    )
    monkeypatch.setattr(bedrock, "_post", post)
    with pytest.raises(TimeoutError):
        await bedrock.call(
            "converse", {"modelId": "m"}, customer.Customer(), request_timeout_s=0.02, attempts=3
        )
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_deadline_closes_a_response_that_keeps_making_progress(monkeypatch):
    closed = asyncio.Event()
    chunks = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunks.append(b'{"ok":')
            yield chunks[-1]
            for _ in range(10):
                await asyncio.sleep(0.02)
                chunks.append(b" ")
                yield chunks[-1]
            yield b"true}"

        async def aclose(self):
            closed.set()

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Stream()),
        )
    )
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    monkeypatch.setattr(
        bedrock.asyncio,
        "to_thread",
        AsyncMock(
            return_value=("https://bedrock.example/", b"{}", {}),
        ),
    )
    async with client:
        with pytest.raises(TimeoutError):
            await bedrock.call(
                "converse",
                {"modelId": "m"},
                customer.Customer(),
                request_timeout_s=0.05,
                attempts=1,
            )
    assert chunks
    assert closed.is_set()


@pytest.mark.asyncio
async def test_cancelled_pending_signer_cannot_send_model_content(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def sign(*args):
        started.set()
        try:
            if not release.wait(10):
                raise AssertionError("signing fixture was not released")
            return "https://bedrock.example/", b"{}", {}
        finally:
            finished.set()

    post = AsyncMock(return_value=bedrock._Reply({"ok": True}, {}))
    monkeypatch.setattr(bedrock, "_signed", sign)
    monkeypatch.setattr(bedrock, "_post", post)
    task = asyncio.create_task(
        bedrock.call(
            "converse", {"modelId": "m"}, customer.Customer(), request_timeout_s=30, attempts=1
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        await asyncio.gather(task, return_exceptions=True)
    post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [0, -1, float("inf"), float("nan")])
async def test_native_request_deadline_must_be_positive_and_finite(monkeypatch, budget):
    signer = AsyncMock()
    monkeypatch.setattr(bedrock.asyncio, "to_thread", signer)
    with pytest.raises(ValueError, match="positive finite timeout"):
        await bedrock.call(
            "converse", {"modelId": "m"}, customer.Customer(), request_timeout_s=budget, attempts=1
        )
    signer.assert_not_called()
