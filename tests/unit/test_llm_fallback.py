"""server/llm/fallback.py — another model answers when the primary's provider
is down after its own retries; request-side errors still propagate."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from server.llm.client import LLMResult
from server.llm.fallback import FallbackLLMClient, hands_over, is_provider_outage


class _Client:
    def __init__(self, model: str, *, raise_exc: BaseException | None = None) -> None:
        self.provider = "fake"
        self.model = model
        self.raise_exc = raise_exc
        self.calls = 0

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return LLMResult(
            text=f"from {self.model}",
            tokens_in=1,
            tokens_out=1,
            provider=self.provider,
            model=self.model,
        )


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.example/v1")
    return httpx.HTTPStatusError(
        f"{status}", request=req, response=httpx.Response(status, request=req)
    )


@pytest.mark.parametrize(
    "exc, outage",
    [
        (_http_error(429), True),
        (_http_error(503), True),
        (_http_error(500), True),
        (_http_error(529), True),  # Anthropic "overloaded", which its client retries
        (httpx.ConnectTimeout("t"), True),
        (httpx.ReadError("r"), True),
        (TimeoutError(), True),
        (_http_error(400), False),
        (_http_error(401), False),
        (_http_error(404), False),
        (ValueError("bad json"), False),
    ],
)
def test_outage_predicate_matches_the_providers_retry_class(exc, outage) -> None:
    assert is_provider_outage(exc) is outage


async def test_primary_answers_when_healthy() -> None:
    p, s = _Client("strong"), _Client("cheap")
    fb = FallbackLLMClient(p, s)
    res = await fb.complete("sys", "user")
    assert res.model == "strong" and s.calls == 0 and fb.fallbacks == 0
    assert fb.model == "strong" and fb.provider == "fake"


async def test_outage_falls_through_to_secondary_and_is_counted(monkeypatch) -> None:
    from server.observability import metrics

    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(metrics, "record_llm_fallback", lambda **kw: recorded.append(kw))
    p, s = _Client("strong", raise_exc=_http_error(503)), _Client("cheap")
    fb = FallbackLLMClient(p, s)
    res = await fb.complete("sys", "user", max_tokens=99)
    # The served model rides the result so cost and audit attribute to it.
    assert res.model == "cheap" and res.text == "from cheap"
    assert p.calls == 1 and s.calls == 1 and fb.fallbacks == 1
    assert recorded == [{"from_model": "strong", "to_model": "cheap", "reason": "http_503"}]


async def test_configuration_errors_do_not_fall_back() -> None:
    # 401/403/404 are configuration; 400 hands over (see the block at the end
    # of this file).
    p, s = _Client("strong", raise_exc=_http_error(404)), _Client("cheap")
    fb = FallbackLLMClient(p, s)
    with pytest.raises(httpx.HTTPStatusError):
        await fb.complete("sys", "user")
    assert s.calls == 0 and fb.fallbacks == 0


async def test_secondary_failure_propagates() -> None:
    p = _Client("strong", raise_exc=_http_error(503))
    s = _Client("cheap", raise_exc=_http_error(502))
    with pytest.raises(httpx.HTTPStatusError):
        await FallbackLLMClient(p, s).complete("sys", "user")


def test_selector_wraps_when_the_setting_names_another_model(monkeypatch) -> None:
    from server.llm import client as llm_client
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "llm_model", "fake-a")
    monkeypatch.setattr(settings, "llm_fallback_model", "fake-b")
    llm_client.reset()
    try:
        default = llm_client.get_llm_client()
        assert isinstance(default, FallbackLLMClient)
        assert default.model == "fake-a" and default.secondary.model == "fake-b"
        over = llm_client.get_llm_client_for_model("fake-c")
        assert isinstance(over, FallbackLLMClient)
        assert over.model == "fake-c" and over.secondary.model == "fake-b"
        # A fallback never chains: the secondary is the plain client.
        assert not isinstance(over.secondary, FallbackLLMClient)
        # The fallback model itself is not wrapped in itself.
        same = llm_client.get_llm_client_for_model("fake-b")
        assert not isinstance(same, FallbackLLMClient) and same.model == "fake-b"
        # Cached: the same wrapped object comes back.
        assert llm_client.get_llm_client_for_model("fake-c") is over
    finally:
        llm_client.reset()


def test_selector_is_unchanged_without_the_setting(monkeypatch) -> None:
    from server.llm import client as llm_client
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "llm_fallback_model", "")
    llm_client.reset()
    try:
        assert not isinstance(llm_client.get_llm_client(), FallbackLLMClient)
        assert not isinstance(llm_client.get_llm_client_for_model("fake-x"), FallbackLLMClient)
    finally:
        llm_client.reset()


# ---------------------------------------------------------------------------
# A request the primary's provider REJECTED (400/413/422) is
# handed over too — the secondary parses a different payload against a
# different prompt filter; configuration errors (401/403/404) stay hard.
# ---------------------------------------------------------------------------


def _http_error_with_body(status: int, body: dict) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.example/v1")
    resp = httpx.Response(status, json=body, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


@pytest.mark.parametrize(
    "exc, over",
    [
        (_http_error(400), True),
        (_http_error(413), True),
        (_http_error(422), True),
        (_http_error(429), True),
        (_http_error(503), True),
        (httpx.ReadError("r"), True),
        (_http_error(401), False),
        (_http_error(403), False),
        (_http_error(404), False),
        (ValueError("bad json"), False),
    ],
)
def test_hand_over_predicate_adds_the_rejected_request_class(exc, over) -> None:
    assert hands_over(exc) is over
    # The outage predicate itself is unchanged: 400 is not an outage.
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 413, 422):
        assert is_provider_outage(exc) is False


async def test_rejected_request_is_answered_by_the_secondary_with_the_body_code(
    monkeypatch,
) -> None:
    from server.observability import metrics

    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(metrics, "record_llm_fallback", lambda **kw: recorded.append(kw))
    exc = _http_error_with_body(
        400, {"error": {"code": "invalid_prompt", "type": "invalid_request_error", "message": "m"}}
    )
    p, s = _Client("strong", raise_exc=exc), _Client("cheap")
    fb = FallbackLLMClient(p, s)
    res = await fb.complete("sys", "user")
    assert res.model == "cheap" and s.calls == 1 and fb.fallbacks == 1
    assert recorded and recorded[0]["reason"] == "http_400_invalid_prompt"


async def test_auth_failure_still_propagates(monkeypatch) -> None:
    p, s = _Client("strong", raise_exc=_http_error(401)), _Client("cheap")
    fb = FallbackLLMClient(p, s)
    with pytest.raises(httpx.HTTPStatusError):
        await fb.complete("sys", "user")
    assert s.calls == 0 and fb.fallbacks == 0


# --- what never hands over --------------------------------------------------------


@pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt()])
async def test_cancellation_and_interrupts_propagate_without_a_fallback(
    exc: BaseException,
) -> None:
    # The wrapper catches BaseException to read the reason; a cancelled request
    # must not spend a second provider call on an answer nobody awaits.
    p, s = _Client("strong", raise_exc=exc), _Client("cheap")
    fb = FallbackLLMClient(p, s)
    with pytest.raises(type(exc)):
        await fb.complete("sys", "user")
    assert s.calls == 0 and fb.fallbacks == 0


async def test_a_broken_metric_does_not_cost_the_answer(monkeypatch) -> None:
    from server.observability import metrics

    def _boom(**_: Any) -> None:
        raise RuntimeError("meter down")

    monkeypatch.setattr(metrics, "record_llm_fallback", _boom)
    p, s = _Client("strong", raise_exc=_http_error(503)), _Client("cheap")
    res = await FallbackLLMClient(p, s).complete("sys", "user")
    assert res.model == "cheap"
