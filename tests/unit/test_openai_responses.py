"""The `openai` provider speaks the Responses API: what one call sends to
`<openai_base_url>/responses`, how the reply is read, and that every job's OpenAI model
gets this client. The reply bodies follow OpenAI's documented Responses API shapes; no
live call is made. Fake HTTP; no network."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.llm import client as llm_client
from server.llm import openai as oa
from server.llm import traits
from server.llm.fallback import FallbackLLMClient
from server.memory.domain.conflict_resolution import RESPONSE_FORMAT


def _message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "status": "completed", "content": list(parts)}


def _text(text: str) -> dict[str, Any]:
    return {"type": "output_text", "text": text, "annotations": []}


_OK = {
    "object": "response",
    "status": "completed",
    "output": [_message(_text("ok"))],
    "usage": {"input_tokens": 7, "output_tokens": 1, "total_tokens": 8},
}


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.body = body or _OK
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def post(self, url, *, headers, json, **_):
        self.calls.append((url, headers, json))
        return _Resp(self.body)


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> _Client:
    traits.reset_for_tests()
    llm_client.reset()
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    stub = _Client()
    monkeypatch.setattr(oa, "_http_client", lambda: stub)
    yield stub
    llm_client.reset()


def _send(stub: _Client, **kw: Any) -> tuple[Any, dict[str, Any]]:
    args = {"system": "Answer in JSON.", "user": "the turn", "max_tokens": 200, **kw}
    result = asyncio.run(oa.OpenAILLMClient("gpt-x").complete(**args))
    return result, stub.calls[-1][2]


def test_a_call_goes_to_the_responses_root(_setup: _Client, monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "openai_base_url", "https://gateway.example/v1/")
    _send(_setup)
    ((url, headers, _),) = _setup.calls
    assert url == "https://gateway.example/v1/responses"
    assert headers["Authorization"] == "Bearer k"


def test_the_system_prompt_is_the_first_input_message_and_the_user_turn_follows(
    _setup: _Client,
) -> None:
    _, payload = _send(_setup)
    assert payload["input"] == [
        {"role": "developer", "content": "Answer in JSON."},
        {"role": "user", "content": "the turn"},
    ]
    assert list(payload)[:2] == ["model", "input"]


def test_no_system_prompt_sends_only_the_user_turn(_setup: _Client) -> None:
    _, payload = _send(_setup, system="")
    assert payload["input"] == [{"role": "user", "content": "the turn"}]


def test_the_request_maps_the_cap_effort_and_temperature(_setup: _Client) -> None:
    _, payload = _send(_setup)
    assert payload["model"] == "gpt-x"
    assert payload["max_output_tokens"] == 200
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["temperature"] == 0.0


def test_the_provider_keeps_no_copy_and_nothing_chains_or_calls_tools(_setup: _Client) -> None:
    _, payload = _send(_setup, response_format={"type": "json_object"})
    assert payload["store"] is False
    for name in (
        "instructions",
        "previous_response_id",
        "tools",
        "prompt",
        "messages",
        "response_format",
        "max_completion_tokens",
        "max_tokens",
        "reasoning_effort",
    ):
        assert name not in payload


def test_no_temperature_asked_sends_none(_setup: _Client) -> None:
    _, payload = _send(_setup, temperature=None)
    assert "temperature" not in payload


def test_json_mode_is_the_text_format(_setup: _Client) -> None:
    _, payload = _send(_setup, response_format={"type": "json_object"})
    assert payload["text"] == {"format": {"type": "json_object"}}
    _, plain = _send(_setup)
    assert "text" not in plain


def test_a_json_schema_sits_flat_in_the_text_format(_setup: _Client) -> None:
    _, payload = _send(_setup, response_format=RESPONSE_FORMAT)
    spec = RESPONSE_FORMAT["json_schema"]
    assert payload["text"] == {
        "format": {
            "type": "json_schema",
            "name": spec["name"],
            "strict": spec["strict"],
            "schema": spec["schema"],
        }
    }


def test_the_reply_joins_the_message_text_and_skips_reasoning(_setup: _Client) -> None:
    _setup.body = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "x"},
            _message(_text('{"a": '), _text("1}")),
            _message(_text("")),
        ],
        "usage": {
            "input_tokens": 2000,
            "input_tokens_details": {"cached_tokens": 1500},
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 120},
        },
    }
    result, _ = _send(_setup)
    assert result.text == '{"a": 1}'
    assert (result.tokens_in, result.tokens_out, result.cached_input_tokens) == (2000, 300, 1500)
    assert (result.provider, result.model, result.stop_reason) == ("openai", "gpt-x", "stop")
    # The reasoning count becomes the model's reserve over later caps.
    assert traits.traits("gpt-x").reserve == 240


def test_a_cached_count_over_the_prompt_is_clamped(_setup: _Client) -> None:
    _setup.body = {
        **_OK,
        "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 99}},
    }
    result, _ = _send(_setup)
    assert result.cached_input_tokens == 10


@pytest.mark.parametrize(
    ("body", "stop"),
    [
        ({"status": "completed", "output": [_message(_text("ok"))]}, "stop"),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [_message(_text("partial"))],
            },
            "length",
        ),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output": [_message(_text("partial"))],
            },
            "content_filter",
        ),
        (
            {
                "status": "completed",
                "output": [_message({"type": "refusal", "refusal": "I can't help with that."})],
            },
            "refusal",
        ),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_messages"},
                "output": [_message(_text("partial"))],
            },
            "max_messages",
        ),
        ({"status": "failed", "output": []}, "failed"),
        ({"output": [_message(_text("ok"))]}, ""),
    ],
)
def test_the_stop_reason_is_in_the_words_callers_read(
    _setup: _Client, body: dict[str, Any], stop: str
) -> None:
    _setup.body = {**body, "usage": {"input_tokens": 1, "output_tokens": 1}}
    result, _ = _send(_setup)
    assert result.stop_reason == stop


def test_a_refusal_carries_no_text(_setup: _Client) -> None:
    _setup.body = {
        "status": "completed",
        "output": [_message({"type": "refusal", "refusal": "I can't help with that."})],
        "usage": {"input_tokens": 1, "output_tokens": 5},
    }
    result, _ = _send(_setup)
    assert (result.text, result.stop_reason) == ("", "refusal")


def _role_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The client each job with an OpenAI model is given, built as that job builds it."""
    from server.memory.app import conflict_resolver
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "gpt-main")
    monkeypatch.setattr(settings, "llm_fallback_model", "gpt-fallback")
    monkeypatch.setattr(settings, "retrieval_judge_model", "gpt-judge")
    monkeypatch.setattr(settings, "session_brief_llm_model", "gpt-brief")
    monkeypatch.setattr(settings, "code_ask_llm_model", "gpt-code")
    monkeypatch.setattr(settings, "conflict_resolver_model", "gpt-resolver")
    main = llm_client.get_llm_client()
    assert isinstance(main, FallbackLLMClient)
    return {
        "llm_model": main.primary,
        "llm_fallback_model": main.secondary,
        "retrieval_judge_model": llm_client.get_llm_client_for_model(
            settings.retrieval_judge_model
        ),
        "session_brief_llm_model": llm_client.get_llm_client_for_model(
            settings.session_brief_llm_model
        ),
        "code_ask_llm_model": llm_client.get_llm_client_for_model(settings.code_ask_llm_model),
        "conflict_resolver_model": conflict_resolver._client(),
    }


def test_every_jobs_openai_model_runs_on_the_responses_api(
    _setup: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients = _role_clients(monkeypatch)
    for role, client in clients.items():
        if isinstance(client, FallbackLLMClient):
            client = client.primary
        assert isinstance(client, oa.OpenAILLMClient), role
        asyncio.run(client.complete("s", "u", max_tokens=10))
        url, _, payload = _setup.calls[-1]
        assert url.endswith("/responses") and "input" in payload, role
    # The resolver asks for its own effort, as a Responses `reasoning` field.
    assert _setup.calls[-1][2]["reasoning"] == {"effort": "low"}


def test_a_failing_call_marks_openai_responses_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from server.observability import provider_health

    class _Refuses:
        async def post(self, url, **_):
            request = httpx.Request("POST", url)
            return httpx.Response(401, json={"error": {"code": "invalid_api_key"}}, request=request)

    monkeypatch.setattr(oa, "_http_client", lambda: _Refuses())
    provider_health.reset()
    with pytest.raises(httpx.HTTPStatusError, match="from openai:responses"):
        asyncio.run(oa.OpenAILLMClient("gpt-x").complete("s", "u", max_tokens=10))
    assert provider_health.status()["providers"]["openai:responses"]["failing"] is True


def test_a_key_check_calls_the_responses_api(_setup: _Client) -> None:
    from server.insights.app import provider_validation

    asyncio.run(provider_validation._probe_llm("openai", "gpt-x", "candidate-key"))
    ((url, headers, _),) = _setup.calls
    assert url.endswith("/responses")
    assert headers["Authorization"] == "Bearer candidate-key"
