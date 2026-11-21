"""The OpenAI client learns each model's Responses API request shape from its refusals:
a refused field is dropped and never sent again, a refused effort settles on the lowest
one the model lists, and reasoning tokens the model reports become room over the
caller's cap. Documented sampling restrictions are tested in
test_llm_sampling_capabilities.py.

The error bodies follow OpenAI's documented error shape (`error.message`, `type`,
`param`, `code`) with the messages the Responses API returns; no live call is made."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from server.llm import openai as oa
from server.llm import traits

_OK = {
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def _unsupported(param: str, code: str | None = "unsupported_parameter") -> dict:
    return {
        "error": {
            "message": f"Unsupported parameter: '{param}' is not supported with this model.",
            "type": "invalid_request_error",
            "param": param,
            "code": code,
        }
    }


def _refused(value: str, supported: str) -> dict:
    return {
        "error": {
            "message": f"Unsupported value: '{value}' is not supported with the 'gpt-x' "
            f"model. Supported values are: {supported}.",
            "type": "invalid_request_error",
            "param": "reasoning.effort",
            "code": "unsupported_value",
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
        self.request = httpx.Request("POST", "https://api.openai.com/v1/responses")

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=self.request, response=self)  # type: ignore[arg-type]


def _complete(
    model: str, monkeypatch, responses: list[_Resp], calls: int = 1, max_tokens: int = 100
) -> list[dict]:
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
        asyncio.run(client.complete("s", "u", max_tokens=max_tokens))
    return sent


def test_a_model_that_refuses_temperature_learns_it_once(monkeypatch) -> None:
    sent = _complete(
        "gpt-new",
        monkeypatch,
        [_Resp(400, _unsupported("temperature", None)), _Resp(200, _OK), _Resp(200, _OK)],
        calls=2,
    )
    assert sent[0]["temperature"] == 0.0
    assert "temperature" not in sent[1] and "temperature" not in sent[2]
    assert len(sent) == 3


def test_a_model_without_reasoning_drops_the_reasoning_field(monkeypatch) -> None:
    sent = _complete(
        "gpt-plain",
        monkeypatch,
        [_Resp(400, _unsupported("reasoning.effort")), _Resp(200, _OK), _Resp(200, _OK)],
        calls=2,
    )
    assert sent[0]["reasoning"] == {"effort": "none"}
    assert "reasoning" not in sent[1] and "reasoning" not in sent[2]
    assert sent[2]["temperature"] == 0.0


def test_a_refused_effort_settles_on_the_lowest_supported_and_is_remembered(monkeypatch) -> None:
    sent = _complete(
        "gpt-sol-like",
        monkeypatch,
        [
            _Resp(400, _refused("none", "'low', 'medium', 'high', 'xhigh', and 'max'")),
            _Resp(200, _OK),
            _Resp(200, _OK),
        ],
        calls=2,
    )
    assert [s["reasoning"]["effort"] for s in sent] == ["none", "low", "low"]


def test_a_refused_effort_without_a_supported_list_steps_up_one(monkeypatch) -> None:
    body = _refused("none", "")
    body["error"]["message"] = "Unsupported value: 'none' is not supported with this model."
    sent = _complete("gpt-x", monkeypatch, [_Resp(400, body), _Resp(200, _OK)])
    assert sent[1]["reasoning"] == {"effort": "minimal"}


def test_an_unsupported_value_of_another_field_is_not_an_effort(monkeypatch) -> None:
    body = {
        "error": {
            "message": "Unsupported value: 'auto' is not supported with this model.",
            "param": "truncation",
            "code": "unsupported_value",
        }
    }
    with pytest.raises(httpx.HTTPStatusError):
        _complete("gpt-x", monkeypatch, [_Resp(400, body)])
    assert traits.traits("gpt-x").effort == "" and not traits.traits("gpt-x").refused


def test_a_refused_cap_fails_the_call_rather_than_going_uncapped(monkeypatch) -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _complete("gpt-x", monkeypatch, [_Resp(400, _unsupported("max_output_tokens"))])
    assert not traits.traits("gpt-x").refused


def test_a_cap_over_the_models_limit_is_clamped_to_it(monkeypatch) -> None:
    body = {
        "error": {
            "message": "max_output_tokens is too large: 100. This model supports at most "
            "64 output tokens, whereas you provided 100.",
            "type": "invalid_request_error",
            "param": "max_output_tokens",
            "code": None,
        }
    }
    sent = _complete("gpt-small", monkeypatch, [_Resp(400, body), _Resp(200, _OK)])
    assert sent[1]["max_output_tokens"] == 64


def test_a_cap_below_the_apis_least_is_raised_to_it(monkeypatch) -> None:
    # A 3-token judge's cap would be refused ("Expected a value >= 16").
    sent = _complete("gpt-judge", monkeypatch, [_Resp(200, _OK)], max_tokens=3)
    assert sent[0]["max_output_tokens"] == 16


def test_reported_reasoning_tokens_become_room_over_the_cap(monkeypatch) -> None:
    used = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
        ],
        "usage": {
            "input_tokens": 1,
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 250},
        },
    }
    sent = _complete("gpt-thinks", monkeypatch, [_Resp(200, used), _Resp(200, _OK)], calls=2)
    assert sent[0]["max_output_tokens"] == 100
    assert sent[1]["max_output_tokens"] == 100 + 500


def test_a_reply_starved_by_reasoning_is_sent_once_more_with_room(monkeypatch) -> None:
    from server.settings import settings

    starved = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning", "summary": []}],
        "usage": {
            "input_tokens": 1,
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 100},
        },
    }
    sent = _complete("gpt-thinks", monkeypatch, [_Resp(200, starved), _Resp(200, _OK)])
    assert len(sent) == 2
    assert sent[1]["max_output_tokens"] == 100 + settings.llm_hidden_reasoning_reserve_max


def test_a_cut_reply_with_text_is_not_starved(monkeypatch) -> None:
    cut = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "par"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 100},
    }
    sent = _complete("gpt-x", monkeypatch, [_Resp(200, cut)])
    assert len(sent) == 1


def test_other_400s_still_raise(monkeypatch) -> None:
    body = {
        "error": {
            "message": "Response input messages must contain the word 'json' in some form "
            "to use 'text.format' of type 'json_object'.",
            "type": "invalid_request_error",
            "param": "input",
            "code": None,
        }
    }
    with pytest.raises(httpx.HTTPStatusError):
        _complete("gpt-x", monkeypatch, [_Resp(400, body)])
    assert not traits.traits("gpt-x").refused


def test_a_needed_field_named_by_the_param_keeps_the_fields_the_message_mentions(
    monkeypatch,
) -> None:
    # The message mentions `reasoning`, but the param says the error is the input's.
    body = {
        "error": {
            "message": "reasoning input items can only be provided to a reasoning model.",
            "param": "input",
        }
    }
    with pytest.raises(httpx.HTTPStatusError):
        _complete("gpt-x", monkeypatch, [_Resp(400, body)])
    assert not traits.traits("gpt-x").refused


def test_a_refused_pin_the_models_floor_does_not_reach_drops_the_field(monkeypatch) -> None:
    from server.settings import settings

    # 'minimal' is refused while a lower effort is listed: the floor stays below the pin,
    # so sending the pin again would be refused again.
    monkeypatch.setattr(settings, "llm_openai_reasoning_effort", "minimal")
    refused = _refused("minimal", "'none', 'low', 'medium', 'high', 'xhigh', and 'max'")
    sent = _complete("gpt-x", monkeypatch, [_Resp(400, refused), _Resp(200, _OK)])
    assert sent[0]["reasoning"] == {"effort": "minimal"}
    assert len(sent) == 2 and "reasoning" not in sent[1]


def test_a_pin_below_the_models_floor_is_raised_to_it(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_openai_reasoning_effort", "none")
    refused = _refused("none", "'low', 'medium', and 'high'")
    sent = _complete("gpt-x", monkeypatch, [_Resp(400, refused), _Resp(200, _OK)])
    assert [s["reasoning"]["effort"] for s in sent] == ["none", "low"]


def test_a_clients_refused_effort_drops_the_field_too(monkeypatch) -> None:
    sent: list[dict] = []
    responses = [
        _Resp(400, _refused("low", "'none', 'medium', and 'high'")),
        _Resp(200, _OK),
    ]

    class _Client:
        async def post(self, url, headers=None, json=None, **_kw):
            sent.append(dict(json))
            return responses.pop(0)

    monkeypatch.setattr(oa, "_http_client", lambda: _Client())
    monkeypatch.setattr(oa, "_raise_for_status", lambda r, _label: r.raise_for_status())
    client = oa.OpenAILLMClient.__new__(oa.OpenAILLMClient)
    client.model, client._api_key, client.reasoning_effort = "gpt-x", "k", "low"
    asyncio.run(client.complete("s", "u", max_tokens=100))
    assert sent[0]["reasoning"] == {"effort": "low"}
    assert len(sent) == 2 and "reasoning" not in sent[1]


def test_pinned_effort_wins(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_openai_reasoning_effort", "high")
    sent = _complete("gpt-x", monkeypatch, [_Resp(200, _OK)])
    assert sent[0]["reasoning"] == {"effort": "high"}


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
    assert "openai:responses" not in provider_health.status()["providers"]


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
    assert sent[1]["max_output_tokens"] >= 100 + 2 * traits.STARVED_FLOOR


def test_a_client_effort_replaces_the_pin_but_not_the_models_floor(monkeypatch) -> None:
    from server.settings import settings

    def effort(wanted: str = "") -> str:
        call = oa.Call(
            system="s",
            user="u",
            max_tokens=10,
            response_format=None,
            temperature=0.0,
            effort=wanted,
        )
        return oa.Responses().payload("gpt-x", call)["reasoning"]["effort"]

    monkeypatch.setattr(settings, "llm_openai_reasoning_effort", "none")
    assert effort("low") == "low"
    assert effort() == "none"
    traits.traits("gpt-x").effort = "medium"
    assert effort("low") == "medium"


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
