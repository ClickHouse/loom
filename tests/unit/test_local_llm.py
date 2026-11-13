"""LOOM_LLM_PROVIDER=local: a chat-completions server the operator runs, called at
LOOM_LOCAL_LLM_BASE_URL with no key. Fake HTTP; no network."""

from __future__ import annotations

import asyncio

import pytest

from server.core.llm_provider import resolve_llm_provider
from server.llm import client as llm_client
from server.llm import openai as oa
from server.llm import traits
from server.llm.local import LocalLLMClient


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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    async def post(self, url, *, headers, json, **_):
        self.calls.append((url, headers, json))
        return _Resp(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1},
            }
        )


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> _Client:
    from server.settings import settings

    traits.reset_for_tests()
    llm_client.reset()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "local_llm_base_url", "http://models.internal:11434/v1/")
    stub = _Client()
    monkeypatch.setattr(oa, "_http_client", lambda: stub)
    return stub


def test_a_call_goes_to_the_local_root_with_no_key(_setup: _Client) -> None:
    result = asyncio.run(LocalLLMClient("qwen2.5:3b").complete("s", "u", max_tokens=10))
    ((url, headers, payload),) = _setup.calls
    assert url == "http://models.internal:11434/v1/chat/completions"
    assert "Authorization" not in headers
    assert payload["model"] == "qwen2.5:3b"
    # Ollama ignores the newer cap field without refusing it.
    assert payload["max_tokens"] >= 10 and "max_completion_tokens" not in payload
    assert (result.text, result.provider, result.model) == ("ok", "local", "qwen2.5:3b")


def test_the_openai_client_still_calls_openai(
    _setup: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    asyncio.run(oa.OpenAILLMClient("gpt-x").complete("s", "u", max_tokens=10))
    ((url, headers, payload),) = _setup.calls
    assert url == "https://api.openai.com/v1/chat/completions"
    assert "max_completion_tokens" in payload and "max_tokens" not in payload
    assert headers["Authorization"] == "Bearer k"


def test_local_needs_no_key_so_it_never_falls_back_to_the_fake() -> None:
    assert not llm_client.provider_key_missing("local")
    built = llm_client._build_llm_client("local", "qwen2.5:3b", fake_label="f", warn="w")
    assert isinstance(built, LocalLLMClient)


def test_a_stage_model_on_a_local_deployment_runs_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "local")
    assert llm_client.get_llm_client_for_model("llama3.2:3b").provider == "local"
    # A hosted model's name still routes to its provider (here keyless, so the fake).
    assert llm_client.get_llm_client_for_model("gpt-6-luna").provider == "fake"


def test_local_takes_its_default_model() -> None:
    resolved = resolve_llm_provider("local", "", env={})
    assert (resolved.provider, resolved.model) == ("local", "qwen2.5:3b")


def test_the_local_root_must_be_an_http_url() -> None:
    from server.settings import Settings

    with pytest.raises(ValueError, match="LOOM_LOCAL_LLM_BASE_URL must be an http"):
        Settings(_env_file=None, local_llm_base_url="localhost:11434")  # type: ignore[call-arg]


def test_a_required_gateway_refuses_the_local_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "local")
    monkeypatch.setattr(settings, "inference_gateway_required", True)
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "openai_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "anthropic_base_url", "https://gateway.example")
    refusal = settings.gateway_refusal()
    assert "LOOM_LOCAL_LLM_BASE_URL" in refusal and "models.internal" in refusal


def test_a_required_gateway_ignores_the_local_root_of_another_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "inference_gateway_required", True)
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "openai_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "anthropic_base_url", "https://gateway.example")
    assert "LOOM_LOCAL_LLM_BASE_URL" not in settings.gateway_refusal()
