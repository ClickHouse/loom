"""Both LLM clients wait `llm_request_timeout_s` for a reply: one generous
deadline for a hung connection, whatever the model's speed."""

from __future__ import annotations

import asyncio

import pytest

from server.llm import anthropic as an
from server.llm import openai as oa
from server.llm import traits


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, body: dict) -> None:
        self.body = body
        self.timeouts: list[float] = []

    async def post(self, url, **kw):
        self.timeouts.append(kw["timeout"])
        return _Resp(self.body)


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    traits.reset_for_tests()
    monkeypatch.setattr(settings, "llm_request_timeout_s", 123.0)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")


def test_openai_waits_the_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Client({"choices": [{"message": {"content": "ok"}}], "usage": {}})
    monkeypatch.setattr(oa, "_http_client", lambda: stub)
    asyncio.run(oa.OpenAILLMClient("gpt-x").complete("s", "u", max_tokens=10))
    assert stub.timeouts == [123.0]


def test_anthropic_waits_the_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Client({"content": [{"type": "text", "text": "ok"}], "usage": {}})
    monkeypatch.setattr(an, "_http_client", lambda: stub)
    asyncio.run(an.AnthropicLLMClient("claude-x").complete("s", "u", max_tokens=10))
    assert stub.timeouts == [123.0]
