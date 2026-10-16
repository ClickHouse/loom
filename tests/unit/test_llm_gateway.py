"""Loom's own model calls through an inference gateway: API roots that point
at it, and a workload token exchanged there instead of a provider key."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.llm import gateway
from server.settings import Settings, settings


class _Gateway:
    """The gateway's token endpoint: answers with tokens it numbers, or fails."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.down = False
        self.life = 900

    async def post(self, url: str, *, data: dict[str, str], **_: Any) -> httpx.Response:
        self.sent.append(data)
        if self.down:
            raise httpx.ConnectError("gateway away")
        body = {"access_token": f"gw-{len(self.sent)}", "expires_in": self.life}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Gateway:
    token = tmp_path / "token"
    token.write_text("projected-sa-token\n")
    monkeypatch.setattr(settings, "inference_token_url", "https://gateway.example/oauth/token")
    monkeypatch.setattr(settings, "inference_workload_token_file", str(token))
    monkeypatch.setattr(settings, "inference_token_scope", "chat embeddings")
    fake = _Gateway()
    monkeypatch.setattr("server.clients._http.get_client", lambda: fake)
    gateway._reset_for_tests()
    yield fake
    gateway._reset_for_tests()


def test_without_an_exchange_the_provider_key_is_the_bearer() -> None:
    assert asyncio.run(gateway.bearer("sk-provider")) == "sk-provider"


def test_the_workload_token_is_exchanged_once_and_reused(gw: _Gateway) -> None:
    assert asyncio.run(gateway.bearer("")) == "gw-1"
    assert asyncio.run(gateway.bearer("")) == "gw-1"
    assert gw.sent == [
        {
            "grant_type": gateway.TOKEN_EXCHANGE,
            "subject_token": "projected-sa-token",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "scope": "chat embeddings",
        }
    ]


def test_it_is_renewed_halfway_and_the_old_one_serves_while_the_gateway_is_away(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(gateway.time, "time", lambda: now[0])
    assert asyncio.run(gateway.bearer("")) == "gw-1"  # lives 900 s, renewed 300 s before its end
    now[0] += 601
    gw.down = True
    assert asyncio.run(gateway.bearer("")) == "gw-1"  # still good: kept, retried later
    assert asyncio.run(gateway.bearer("")) == "gw-1"
    assert len(gw.sent) == 2  # one failed renewal, then it waits
    now[0] += 31
    gw.down = False
    assert asyncio.run(gateway.bearer("")) == "gw-3"
    now[0] += 900  # gw-3 long gone, and the gateway away again
    gw.down = True
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer(""))


def test_an_unreadable_workload_token_is_an_outage_not_a_crash(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "inference_workload_token_file", "/nonexistent/token")
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer(""))
    assert gw.sent == []


def test_a_gateway_outage_reads_as_a_transport_failure() -> None:
    # So retries and the fallback model handle it as they do any outage.
    assert issubclass(gateway.GatewayUnavailable, httpx.TransportError)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LOOM_OPENAI_BASE_URL": "gateway.example/v1"}, "LOOM_OPENAI_BASE_URL"),
        (
            {"LOOM_INFERENCE_TOKEN_URL": "https://gateway.example/oauth/token"},
            "WORKLOAD_TOKEN_FILE",
        ),
    ],
)
def test_a_mistyped_gateway_setting_fails_at_load(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None)  # type: ignore[call-arg]


class _Recorder:
    def __init__(self, body: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.body = body

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        return httpx.Response(200, json=self.body, request=httpx.Request("POST", url))


def test_chat_and_embeddings_go_to_the_gateway_with_its_token(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.embedding import openai as embed_mod
    from server.llm import openai as chat_mod

    monkeypatch.setattr(settings, "openai_base_url", "https://gateway.example/v1/")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    chat = _Recorder(
        {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    monkeypatch.setattr(chat_mod, "_http_client", lambda: chat)
    asyncio.run(chat_mod.OpenAILLMClient(model="gpt-4o-mini", api_key="").complete("s", "u"))
    assert chat.calls[0]["url"] == "https://gateway.example/v1/chat/completions"
    assert chat.calls[0]["headers"]["Authorization"] == "Bearer gw-1"

    vectors = _Recorder({"data": [{"embedding": [0.0] * 8}], "usage": {"total_tokens": 1}})
    monkeypatch.setattr(embed_mod, "_http_client", lambda: vectors)
    embedder = embed_mod.OpenAIEmbedder(model="text-embedding-3-small", dim=8, api_key="")
    asyncio.run(embedder._post(embedder._payload("text")))
    assert vectors.calls[0]["url"] == "https://gateway.example/v1/embeddings"
    assert vectors.calls[0]["headers"]["Authorization"] == "Bearer gw-1"


def test_anthropic_behind_a_gateway_also_gets_the_key_as_a_bearer(monkeypatch) -> None:
    from server.llm import anthropic as anthropic_mod

    monkeypatch.setattr(settings, "anthropic_base_url", "https://gateway.example")
    recorder = _Recorder({})
    asyncio.run(anthropic_mod._post(recorder, "claude-haiku-4-5", "svc-key", {}))
    call = recorder.calls[0]
    assert call["url"] == "https://gateway.example/v1/messages"
    assert call["headers"]["Authorization"] == "Bearer svc-key"


def test_an_exchanging_deployment_does_not_fall_back_to_the_fake_model(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.llm.client import provider_key_missing

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not provider_key_missing("openai") and not provider_key_missing("anthropic")


def test_anthropic_calls_carry_the_gateway_token(gw: _Gateway, monkeypatch) -> None:
    from server.llm import anthropic as anthropic_mod

    monkeypatch.setattr(settings, "anthropic_base_url", "https://gateway.example")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    recorder = _Recorder(
        {
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
    )
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: recorder)
    client = anthropic_mod.AnthropicLLMClient(model="claude-haiku-4-5", api_key="")
    asyncio.run(client.complete("s", "u"))
    headers = recorder.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer gw-1" and headers["x-api-key"] == "gw-1"


def test_a_gateway_that_refuses_is_asked_again_only_after_a_pause(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No token ever had (a wrong audience, the gateway away at boot): every call
    # must not knock on the token endpoint.
    now = [1000.0]
    monkeypatch.setattr(gateway.time, "time", lambda: now[0])
    gw.down = True
    for _ in range(3):
        with pytest.raises(gateway.GatewayUnavailable):
            asyncio.run(gateway.bearer(""))
    assert len(gw.sent) == 1
    now[0] += 31
    gw.down = False
    assert asyncio.run(gateway.bearer("")) == "gw-2"


def test_a_token_that_is_already_spent_counts_as_no_token(gw: _Gateway) -> None:
    gw.life = 0
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer(""))


@pytest.mark.parametrize(
    ("env", "message"),
    [
        # The gateway's token would go to the provider itself.
        ({"LOOM_LLM_PROVIDER": "openai", "LOOM_LLM_MODEL": "gpt-6-luna"}, "LOOM_OPENAI_BASE_URL"),
        ({"LOOM_INFERENCE_TOKEN_URL": "http://gateway.example/oauth/token"}, "https"),
        ({"LOOM_OPENAI_BASE_URL": "https://"}, "LOOM_OPENAI_BASE_URL"),
    ],
)
def test_a_gateway_set_up_to_leak_or_fail_is_refused_at_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str], message: str
) -> None:
    token = tmp_path / "token"
    token.write_text("t")
    base = {
        "LOOM_INFERENCE_TOKEN_URL": "https://gateway.example/oauth/token",
        "LOOM_INFERENCE_WORKLOAD_TOKEN_FILE": str(token),
    }
    for name, value in {**base, **env}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_health_does_not_call_an_exchanging_deployment_keyless(gw: _Gateway, monkeypatch) -> None:
    from server.observability.problems import HealthState, collect_problems

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-large")
    codes = {p.code for p in collect_problems(settings, HealthState(env={}))}
    assert "llm_key_missing" not in codes and "embedder_key_missing" not in codes
