"""The LongMemEval judge retries a provider failure before an answer is counted WRONG."""

import asyncio

import httpx
import pytest

from bench import longmemeval as lme


def _judge(transport: httpx.MockTransport) -> tuple[str | None, str]:
    async def run() -> tuple[str | None, str]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await lme._llm_judge(
                client,
                "q?",
                "gold",
                "answer",
                "gpt-4o-2024-08-06",
                "k",
                question_type="multi-session",
                abstention=False,
            )

    return asyncio.run(run())


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_s: float) -> None:
        return None

    monkeypatch.setattr(lme.asyncio, "sleep", instant)


def _reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_a_timeout_then_a_reply_is_graded() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return _reply("Yes.")

    assert _judge(httpx.MockTransport(handler)) == ("CORRECT", "Yes.")
    assert len(calls) == 2


def test_rate_limits_are_retried_until_the_attempts_run_out() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json={"error": "slow down"})

    label, why = _judge(httpx.MockTransport(handler))
    assert label is None and why.startswith("judge error: 429")
    assert len(calls) == lme._JUDGE_ATTEMPTS


def test_a_client_error_is_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"error": "bad request"})

    label, _ = _judge(httpx.MockTransport(handler))
    assert label is None and len(calls) == 1
