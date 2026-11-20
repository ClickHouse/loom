"""Model parameter negotiation retains privacy and refuses unrelated errors."""

import asyncio
import json

import httpx
import pytest

from server.clients import bedrock
from server.llm import customer, traits
from server.llm.bedrock import BedrockLLMClient
from server.settings import settings
from tests.unit.clock_fake import ManualClock


def _signed(operation, payload, headers, region):
    body = json.dumps({k: v for k, v in payload.items() if k != "modelId"}).encode()
    return "https://bedrock.example/model/example/converse", body, {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,config,message,parameter",
    [
        (
            400,
            "ValidationException",
            {"temperature": 0},
            "The model returned the following errors: `temperature` is deprecated for this model.",
            "temperature",
        ),
        (400, "ValidationException", {}, "temperature is deprecated for this model", None),
        (
            403,
            "AccessDeniedException",
            {"temperature": 0},
            "temperature is deprecated for this model",
            None,
        ),
        (400, "ValidationException", {"temperature": 0}, "Invalid model identifier", None),
        (
            400,
            "ValidationException",
            {"temperature": 0},
            "temperature must be between 0 and 1",
            None,
        ),
    ],
)
async def test_only_a_sent_rejected_optional_parameter_survives_error_sanitization(
    monkeypatch, status, code, config, message, parameter
):
    def respond(request):
        return httpx.Response(
            status,
            headers={"x-amzn-errortype": code},
            json={"message": message + " private-credential-detail"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    monkeypatch.setattr(bedrock, "_signed", _signed)
    async with client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await bedrock.call(
                "converse",
                {"modelId": "example", "inferenceConfig": config},
                customer.Customer(),
                request_timeout_s=1,
                attempts=1,
            )
    expected = {"code": code}
    if parameter:
        expected["param"] = parameter
    assert caught.value.response.json() == {"error": expected}
    assert "private-credential-detail" not in caught.value.response.text


@pytest.mark.asyncio
async def test_rejected_temperature_is_negotiated_and_remembered(monkeypatch):
    model = "example-negotiated-profile"
    traits.reset_for_tests()
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "temperature" in payload["inferenceConfig"]:
            return httpx.Response(
                400,
                headers={"x-amzn-errortype": "ValidationException"},
                json={"message": "`temperature` is deprecated for this model."},
            )
        return httpx.Response(
            200,
            json={
                "output": {"message": {"content": [{"text": "complete"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 4, "outputTokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    monkeypatch.setattr(bedrock, "_signed", _signed)
    monkeypatch.setattr(settings, "llm_retry_attempts", 1)
    try:
        async with client:
            with customer.system():
                provider = BedrockLLMClient(model)
                first = await provider.complete("", "question", temperature=0)
                second = await provider.complete("", "question", temperature=0)
        assert first.text == second.text == "complete"
        assert len(requests) == 3
        assert requests[0]["inferenceConfig"]["temperature"] == 0
        assert all("temperature" not in r["inferenceConfig"] for r in requests[1:])
        assert traits.traits(model).refused == {"temperature"}
    finally:
        traits.reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,message",
    [
        (400, "ValidationException", "Invalid model identifier"),
        (403, "AccessDeniedException", "`temperature` is deprecated for this model."),
        (400, "ValidationException", "temperature must be between 0 and 1"),
    ],
)
async def test_unrelated_or_authorization_errors_are_never_negotiated(
    monkeypatch, status, code, message
):
    model = "example-refusing-profile"
    traits.reset_for_tests()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"x-amzn-errortype": code}, json={"message": message})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(bedrock, "get_model_client", lambda: client)
    monkeypatch.setattr(bedrock, "_signed", _signed)
    monkeypatch.setattr(settings, "llm_retry_attempts", 1)
    try:
        async with client:
            with customer.system(), pytest.raises(httpx.HTTPStatusError):
                await BedrockLLMClient(model).complete("", "question", temperature=0)
        assert len(requests) == 1 and not traits.traits(model).refused
    finally:
        traits.reset_for_tests()


@pytest.mark.asyncio
async def test_negotiation_shares_one_deadline_and_cancels_the_next_request(monkeypatch):
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    cancelled = asyncio.Event()
    attempts = []
    request = httpx.Request("POST", "https://bedrock.example/model/example/converse")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {"code": "ValidationException", "param": "temperature"},
        },
    )

    async def call(*args, **kwargs):
        attempts.append(True)
        clock.advance(0.06)
        if len(attempts) == 1:
            raise httpx.HTTPStatusError("rejected parameter", request=request, response=response)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(bedrock, "call", call)
    monkeypatch.setattr(settings, "llm_request_timeout_s", 0.1)
    traits.reset_for_tests()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", clock.monotonic)
            with customer.system():
                task = asyncio.create_task(
                    BedrockLLMClient("example-deadline-profile").complete(
                        "", "question", temperature=0
                    )
                )
            for _ in range(10):
                await asyncio.sleep(0)
                if task.done():
                    break
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                pytest.fail("the shared request deadline did not expire")
            with pytest.raises(TimeoutError):
                await task
        assert len(attempts) == 2 and cancelled.is_set()
    finally:
        traits.reset_for_tests()
