"""bench/_http.py: the one JSON POST policy the bench harnesses share."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from bench import _http


def _script(*responses: httpx.Response) -> tuple[list[httpx.Request], Callable[..., Any]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    return seen, handler


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []

    async def fake_async_sleep(s: float) -> None:
        waits.append(s)

    monkeypatch.setattr(_http.time, "sleep", waits.append)
    monkeypatch.setattr(_http.asyncio, "sleep", fake_async_sleep)
    return waits


def test_sync_post_sends_json_and_bearer_and_returns_the_body(slept: list[float]) -> None:
    seen, handler = _script(httpx.Response(200, json={"ok": 1}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        out = _http.post_json(c, "http://loom/v1/memory.set", {"a": 1}, token="t")
    assert out == {"ok": 1} and slept == []
    assert seen[0].headers["authorization"] == "Bearer t"
    assert json.loads(seen[0].content) == {"a": 1}


def test_no_token_leaves_the_clients_own_auth_header(slept: list[float]) -> None:
    seen, handler = _script(httpx.Response(200, json={}))
    with httpx.Client(
        transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer c"}
    ) as c:
        _http.post_json(c, "http://loom/v1/x", {})
    assert seen[0].headers["authorization"] == "Bearer c"


def test_a_429_waits_retry_after_and_does_not_spend_the_5xx_budget(slept: list[float]) -> None:
    seen, handler = _script(
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(429, headers={"Retry-After": "soon"}),
        httpx.Response(429, headers={"Retry-After": "900"}),
        httpx.Response(200, json={"ok": True}),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert _http.post_json(c, "http://loom/v1/x", {}) == {"ok": True}
    assert len(seen) == 4
    assert slept == [3.0, _http.RETRY_AFTER_DEFAULT_S, _http.RETRY_AFTER_MAX_S]


def test_429s_past_the_wait_budget_raise(slept: list[float]) -> None:
    _, handler = _script(*[httpx.Response(429) for _ in range(3)])
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError) as e:
            _http.post(c, "http://loom/v1/x", {}, waits_429=2)
    assert e.value.response.status_code == 429 and len(slept) == 2


def test_a_5xx_is_retried_up_to_attempts_with_backoff_then_raises(slept: list[float]) -> None:
    seen, handler = _script(*[httpx.Response(503, text="table restarting") for _ in range(3)])
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError) as e:
            _http.post(c, "http://loom/v1/x", {}, attempts=3)
    assert len(seen) == 3 and slept == [0.5, 1.0]
    assert "503" in str(e.value) and "table restarting" in str(e.value)


def test_one_attempt_by_default_and_a_4xx_raises_with_the_body(slept: list[float]) -> None:
    seen, handler = _script(httpx.Response(500), httpx.Response(403, json={"code": "DENIED"}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            _http.post(c, "http://loom/v1/x", {})
        with pytest.raises(httpx.HTTPStatusError) as e:
            _http.post(c, "http://loom/v1/x", {}, attempts=5)
    assert len(seen) == 2 and slept == []
    assert "403" in str(e.value) and "DENIED" in str(e.value)


def test_async_post_follows_the_same_policy(slept: list[float]) -> None:
    seen, handler = _script(
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(502),
        httpx.Response(200, json={"n": 2}),
    )

    async def run() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await _http.apost_json(c, "http://loom/v1/x", {"q": 1}, token="t", attempts=2)

    assert asyncio.run(run()) == {"n": 2}
    assert len(seen) == 3 and slept == [_http.RETRY_AFTER_MIN_S, 0.5]
    assert all(r.headers["authorization"] == "Bearer t" for r in seen)


def test_transport_errors_propagate(slept: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.ConnectError):
            _http.post(c, "http://loom/v1/x", {}, attempts=3)
    assert slept == []
