"""The OpenAI client learns each model's request shape from its refusals: a refused
field is dropped and never sent again, a refused effort settles on the lowest one the
model lists, and reasoning tokens the model reports become room over the caller's cap.
No model name decides any of it."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from server.llm import openai as oa
from server.llm import traits

_OK = {
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


def _refused(value: str, supported: str) -> dict:
    return {
        "error": {
            "message": f"Unsupported value: 'reasoning_effort' does not support '{value}' "
            f"with this model. Supported values are: {supported}.",
            "param": "reasoning_effort",
        }
    }


def _unsupported(param: str) -> dict:
    return {
        "error": {
            "message": f"Unsupported parameter: '{param}' is not supported with this model.",
            "param": param,
            "code": "unsupported_parameter",
        }
    }


@pytest.fixture(autouse=True)
def _fresh_traits() -> None:
    traits.reset_for_tests()


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)
        self.request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=self.request, response=self)  # type: ignore[arg-type]


def _complete(model: str, monkeypatch, responses: list[_Resp], calls: int = 1) -> list[dict]:
    sent: list[dict] = []

    class _Client:
        async def post(self, url, headers=None, json=None, **_kw):
            sent.append(dict(json))
            return responses.pop(0)

    monkeypatch.setattr(oa, "_http_client", lambda: _Client())
    monkeypatch.setattr(oa, "_raise_for_status", lambda r, _label: r.raise_for_status())
    client = oa.OpenAILLMClient.__new__(oa.OpenAILLMClient)
    client.model = model
    client._api_key = "k"
    for _ in range(calls):
        asyncio.run(client.complete("s", "u", max_tokens=100))
    return sent


def test_a_model_that_refuses_temperature_learns_it_once(monkeypatch) -> None:
    sent = _complete(
        "gpt-new",
        monkeypatch,
        [_Resp(400, _unsupported("temperature")), _Resp(200, _OK), _Resp(200, _OK)],
        calls=2,
    )
    assert sent[0]["temperature"] == 0.0
    assert "temperature" not in sent[1] and "temperature" not in sent[2]
    assert len(sent) == 3


def test_a_model_without_reasoning_drops_the_effort_field(monkeypatch) -> None:
    sent = _complete(
        "gpt-plain",
        monkeypatch,
        [_Resp(400, _unsupported("reasoning_effort")), _Resp(200, _OK), _Resp(200, _OK)],
        calls=2,
    )
    assert sent[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in sent[1] and "reasoning_effort" not in sent[2]
    assert sent[2]["temperature"] == 0.0


def test_a_refused_effort_settles_on_the_lowest_supported_and_is_remembered(monkeypatch) -> None:
    sent = _complete(
        "gpt-sol-like",
        monkeypatch,
        [
            _Resp(400, _refused("none", "'low', 'medium', 'high', and 'xhigh'")),
            _Resp(200, _OK),
            _Resp(200, _OK),
        ],
        calls=2,
    )
    assert [s["reasoning_effort"] for s in sent] == ["none", "low", "low"]


def test_a_refusal_without_a_supported_list_steps_up_one(monkeypatch) -> None:
    body = {"error": {"message": "reasoning_effort does not support 'none' with this model."}}
    sent = _complete("gpt-x", monkeypatch, [_Resp(400, body), _Resp(200, _OK)])
    assert sent[1]["reasoning_effort"] == "minimal"


def test_an_endpoint_that_calls_the_cap_max_tokens_gets_it(monkeypatch) -> None:
    sent = _complete(
        "local-model",
        monkeypatch,
        [_Resp(400, _unsupported("max_completion_tokens")), _Resp(200, _OK)],
    )
    assert sent[0]["max_completion_tokens"] == 100
    assert sent[1]["max_tokens"] == 100 and "max_completion_tokens" not in sent[1]


def test_reported_reasoning_tokens_become_room_over_the_cap(monkeypatch) -> None:
    used = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 250},
        },
    }
    sent = _complete("gpt-thinks", monkeypatch, [_Resp(200, used), _Resp(200, _OK)], calls=2)
    assert sent[0]["max_completion_tokens"] == 100
    assert sent[1]["max_completion_tokens"] == 100 + 500


def test_a_reply_starved_by_reasoning_is_sent_once_more_with_room(monkeypatch) -> None:
    from server.settings import settings

    starved = {
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 100},
        },
    }
    sent = _complete("gpt-thinks", monkeypatch, [_Resp(200, starved), _Resp(200, _OK)])
    assert len(sent) == 2
    assert sent[1]["max_completion_tokens"] == 100 + settings.llm_hidden_reasoning_reserve_max


def test_other_400s_still_raise(monkeypatch) -> None:
    body = {"error": {"message": "Invalid prompt", "param": "messages"}}
    with pytest.raises(httpx.HTTPStatusError):
        _complete("gpt-x", monkeypatch, [_Resp(400, body)])


def test_pinned_effort_wins(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_openai_reasoning_effort", "high")
    sent = _complete("gpt-x", monkeypatch, [_Resp(200, _OK)])
    assert sent[0]["reasoning_effort"] == "high"


def test_a_cancelled_call_is_not_a_provider_failure(monkeypatch) -> None:
    from server.observability import provider_health

    class _Hangs:
        async def post(self, *a, **kw):
            await asyncio.sleep(3600)

    monkeypatch.setattr(oa, "_http_client", lambda: _Hangs())
    client = oa.OpenAILLMClient.__new__(oa.OpenAILLMClient)
    client.model = "gpt-4o-mini"
    client._api_key = "k"

    async def run() -> None:
        task = asyncio.create_task(client.complete("s", "u", max_tokens=10))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    provider_health.reset()
    asyncio.run(run())
    assert "openai:chat" not in provider_health.status()["providers"]


def test_a_starved_400_is_resent_with_room_for_the_reasoning(monkeypatch) -> None:
    body = {
        "error": {
            "message": "Could not finish the message because max_tokens or model output "
            "limit was reached. Please try again with higher max_tokens.",
            "param": None,
        }
    }
    sent = _complete("gpt-thinks", monkeypatch, [_Resp(400, body), _Resp(200, _OK)])
    assert len(sent) == 2
    assert sent[1]["max_completion_tokens"] >= 100 + 2 * traits.STARVED_FLOOR


def test_a_client_effort_replaces_the_pin_but_not_the_models_floor(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_openai_reasoning_effort", "none")
    assert (
        oa._payload("gpt-x", "s", "u", 10, None, 0.0, "low", "max_completion_tokens")[
            "reasoning_effort"
        ]
        == "low"
    )
    assert (
        oa._payload("gpt-x", "s", "u", 10, None, 0.0, "", "max_completion_tokens")[
            "reasoning_effort"
        ]
        == "none"
    )
    traits.traits("gpt-x").effort = "medium"
    assert (
        oa._payload("gpt-x", "s", "u", 10, None, 0.0, "low", "max_completion_tokens")[
            "reasoning_effort"
        ]
        == "medium"
    )


def test_the_resolver_client_asks_an_openai_model_for_its_effort(monkeypatch) -> None:
    import server.llm.client as llm_client

    monkeypatch.setattr(llm_client, "provider_key_missing", lambda p: False)
    monkeypatch.setattr(oa, "provider_key", lambda p: "k")
    llm_client.reset()
    try:
        c = llm_client.get_llm_client_without_fallback("gpt-6.1-sol", reasoning_effort="low")
        assert isinstance(c, oa.OpenAILLMClient) and c.reasoning_effort == "low"
        plain = llm_client.get_llm_client_without_fallback("gpt-6.1-sol")
        assert plain.reasoning_effort == ""
    finally:
        llm_client.reset()
