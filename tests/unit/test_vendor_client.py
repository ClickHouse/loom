"""The helpers the knowledge-source clients share (server/clients/_vendor.py), over a
mock transport: which error an answer stands for, the token exchange, and the
size-capped read. No error carries the vendor's response body."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from server.clients import _vendor

URL = "https://vendor.example/v1/thing"
SECRET = "doc-title-from-the-body"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _answer(status: int, body: Any = None) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


def _google_reason(reason: str) -> dict[str, Any]:
    return {"error": {"errors": [{"reason": reason, "message": SECRET}]}}


@pytest.mark.parametrize(
    ("status", "body", "raised", "text"),
    [
        (429, {}, _vendor.RateLimited, "thing: rate_limited"),
        (403, _google_reason("userRateLimitExceeded"), _vendor.RateLimited, "thing: rate_limited"),
        (403, _google_reason("dailyLimitExceeded"), _vendor.RateLimited, "thing: rate_limited"),
        (403, _google_reason("rateLimitExceeded"), _vendor.RateLimited, "thing: rate_limited"),
        (
            403,
            {"error": {"status": "RESOURCE_EXHAUSTED"}},
            _vendor.RateLimited,
            "thing: rate_limited",
        ),
        (
            403,
            _google_reason("insufficientPermissions"),
            _vendor.CredentialRefused,
            "thing: http_403",
        ),
        (
            403,
            {"code": "restricted_resource", "message": SECRET},
            _vendor.CredentialRefused,
            "thing: http_403",
        ),
        (401, {"message": SECRET}, _vendor.CredentialRefused, "thing: http_401"),
        (404, {"message": SECRET}, _vendor.NotFound, "thing: http_404"),
        (400, {"message": SECRET}, _vendor.VendorError, "thing: http_400"),
        (503, {"message": SECRET}, _vendor.VendorError, "thing: http_503"),
    ],
)
def test_an_error_answer_maps_to_its_error_without_the_body(
    status: int, body: Any, raised: type, text: str
) -> None:
    with pytest.raises(raised, match=f"^{re.escape(text)}$") as caught:
        _vendor.raise_for_status(httpx.Response(status, json=body), "thing")
    assert caught.value.status == status
    assert SECRET not in str(caught.value)


def test_a_403_whose_body_is_not_json_is_a_refusal() -> None:
    with pytest.raises(_vendor.CredentialRefused, match="^thing: http_403$"):
        _vendor.raise_for_status(httpx.Response(403, text="not json"), "thing")


def test_a_success_raises_nothing() -> None:
    _vendor.raise_for_status(httpx.Response(200, json={}), "thing")
    _vendor.raise_for_status(httpx.Response(204), "thing")


def test_refused_is_a_4xx_and_not_a_5xx_or_no_answer() -> None:
    assert _vendor.NotFound("x", status=404).refused
    assert _vendor.RateLimited("x", status=429).refused
    assert not _vendor.VendorError("x", status=502).refused
    assert not _vendor.VendorError("x").refused


def test_a_request_answers_its_json_object() -> None:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    data = asyncio.run(
        _vendor.request_json(
            _client(_record), "POST", URL, "thing", headers={"X-A": "1"}, json={"q": 1}
        )
    )
    assert data == {"ok": True}
    assert seen[0].method == "POST" and seen[0].headers["X-A"] == "1"
    assert seen[0].content == b'{"q":1}'


@pytest.mark.parametrize(
    ("answer", "text"),
    [
        (httpx.Response(200, content=b"<html>" + SECRET.encode()), "thing: body is not JSON"),
        (httpx.Response(200, json=[SECRET]), "thing: body is not an object"),
    ],
)
def test_an_answer_that_is_not_a_json_object_is_refused(answer: httpx.Response, text: str) -> None:
    with pytest.raises(_vendor.VendorError, match=f"^{text}$") as caught:
        asyncio.run(_vendor.request_json(_client(lambda r: answer), "GET", URL, "thing"))
    assert caught.value.status == 0


def test_a_network_failure_names_its_type_only() -> None:
    def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("vendor.example refused", request=request)

    with pytest.raises(_vendor.VendorError, match="^thing: ConnectError$") as caught:
        asyncio.run(_vendor.request_json(_client(_down), "GET", URL, "thing"))
    assert caught.value.status == 0


@pytest.mark.parametrize(
    ("status", "body", "raised", "text"),
    [
        (400, {"error": "invalid_grant"}, _vendor.CredentialRefused, "token exchange: http_400"),
        (401, {}, _vendor.CredentialRefused, "token exchange: http_401"),
        (403, {}, _vendor.CredentialRefused, "token exchange: http_403"),
        (
            403,
            {"error": "rate_limit_exceeded"},
            _vendor.RateLimited,
            "token exchange: rate_limited",
        ),
        (429, {}, _vendor.RateLimited, "token exchange: rate_limited"),
        (404, {}, _vendor.NotFound, "token exchange: http_404"),
        (500, {}, _vendor.VendorError, "token exchange: http_500"),
        (
            200,
            {"token_type": "Bearer"},
            _vendor.VendorError,
            "token exchange: no access_token in the answer",
        ),
        (
            200,
            {"access_token": ""},
            _vendor.VendorError,
            "token exchange: no access_token in the answer",
        ),
    ],
)
def test_a_token_exchange_failure_says_which(
    status: int, body: Any, raised: type, text: str
) -> None:
    with pytest.raises(raised, match=f"^{re.escape(text)}$") as caught:
        asyncio.run(_vendor.exchange_token(_client(_answer(status, body)), URL, "token exchange"))
    assert caught.value.status == (status if status >= 400 else 0)


def test_a_token_exchange_answers_the_whole_body() -> None:
    body = {"access_token": "tok", "scope": "read"}
    client = _client(_answer(200, body))
    assert asyncio.run(_vendor.exchange_token(client, URL, "token exchange")) == body


def test_a_capped_read_keeps_a_body_at_the_bound_and_stops_past_it() -> None:
    async def _read(size: int, cap: int) -> bytes | None:
        client = _client(lambda r: httpx.Response(200, content=b"x" * size))
        async with client.stream("GET", URL) as resp:
            return await _vendor.read_capped(resp, cap)

    assert asyncio.run(_read(1000, 1000)) == b"x" * 1000
    assert asyncio.run(_read(1001, 1000)) is None
    assert asyncio.run(_read(0, 1000)) == b""


def test_a_capped_get_sends_the_token_and_params_and_refuses_a_body_past_the_bound() -> None:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"x" * 5000)

    client = _client(_record)
    body = asyncio.run(
        _vendor.get_capped(client, URL, "tok", "download", max_bytes=5000, params={"p": "q"})
    )
    assert body == b"x" * 5000
    assert seen[0].headers["Authorization"] == "Bearer tok" and seen[0].url.params["p"] == "q"
    with pytest.raises(_vendor.TooLarge, match="^download: over 4999 bytes$") as caught:
        asyncio.run(_vendor.get_capped(client, URL, "tok", "download", max_bytes=4999))
    assert caught.value.status == 0


def test_a_capped_get_reads_the_error_body_to_tell_a_rate_limit_from_a_refusal() -> None:
    limited = _client(_answer(403, _google_reason("rateLimitExceeded")))
    with pytest.raises(_vendor.RateLimited):
        asyncio.run(_vendor.get_capped(limited, URL, "tok", "download", max_bytes=10))
    refused = _client(_answer(403, _google_reason("cannotDownloadFile")))
    with pytest.raises(_vendor.CredentialRefused, match="^download: http_403$"):
        asyncio.run(_vendor.get_capped(refused, URL, "tok", "download", max_bytes=10))
