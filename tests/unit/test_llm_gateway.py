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

_GW = "https://gateway.example/v1/chat/completions"


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
    assert asyncio.run(gateway.bearer("sk-provider", _GW)) == "sk-provider"


def test_the_workload_token_is_exchanged_once_and_reused(gw: _Gateway) -> None:
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"
    assert gw.sent == [
        {
            "grant_type": gateway.TOKEN_EXCHANGE,
            "subject_token": "projected-sa-token",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "scope": "chat embeddings",
        }
    ]


def test_it_is_renewed_before_its_end_and_the_old_one_serves_while_the_gateway_is_away(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(gateway.time, "time", lambda: now[0])
    assert (
        asyncio.run(gateway.bearer("", _GW)) == "gw-1"
    )  # lives 900 s, renewed 300 s before its end
    now[0] += 601
    gw.down = True
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"  # still good: kept, retried later
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"
    assert len(gw.sent) == 2  # one failed renewal, then it waits
    now[0] += 31
    gw.down = False
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-3"
    now[0] += 900  # gw-3 long gone, and the gateway away again
    gw.down = True
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))


def test_an_unreadable_workload_token_is_an_outage_not_a_crash(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "inference_workload_token_file", "/nonexistent/token")
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))
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
    asyncio.run(embedder._post(embedder._payload("text"), ""))
    assert vectors.calls[0]["url"] == "https://gateway.example/v1/embeddings"
    assert vectors.calls[0]["headers"]["Authorization"] == "Bearer gw-1"


def test_anthropic_behind_a_gateway_also_gets_the_key_as_a_bearer(monkeypatch) -> None:
    from server.llm import anthropic as anthropic_mod

    monkeypatch.setattr(settings, "anthropic_base_url", "https://gateway.example")
    recorder = _Recorder({})
    asyncio.run(anthropic_mod._post(recorder, "claude-haiku-4-5", "svc-key", {}, ""))
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
            asyncio.run(gateway.bearer("", _GW))
    assert len(gw.sent) == 1
    now[0] += 31
    gw.down = False
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-2"


def test_a_token_that_is_already_spent_counts_as_no_token(gw: _Gateway) -> None:
    gw.life = 0
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))


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


@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com/v1/messages",
        "https://API.openai.com/v1/embeddings",
        "https://api.openai.com./v1/chat/completions",  # a trailing dot
        "https://eu.api.openai.com/v1/chat/completions",  # a regional host
    ],
)
def test_the_gateway_token_never_goes_to_a_providers_own_api(gw: _Gateway, url: str) -> None:
    # Whatever path picked the provider: a model named per request, the dashboard's check.
    with pytest.raises(gateway.GatewayUnavailable, match="not the gateway"):
        asyncio.run(gateway.bearer("", url))
    assert gw.sent == []


def test_a_claude_model_named_per_request_does_not_take_the_token_to_anthropic(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.llm import anthropic as anthropic_mod

    recorder = _Recorder({})
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: recorder)
    client = anthropic_mod.AnthropicLLMClient(model="claude-haiku-4-5", api_key="")
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(client.complete("s", "u"))
    assert recorder.calls == []


@pytest.mark.parametrize(
    "env",
    [
        {"LOOM_OPENAI_BASE_URL": "https://gateway.example/v1"},  # Anthropic still its own
        {
            "LOOM_OPENAI_BASE_URL": "https://API.openai.com/v1/",
            "LOOM_ANTHROPIC_BASE_URL": "https://gateway.example",
        },
        {
            "LOOM_OPENAI_BASE_URL": "https://gateway.example/v1",
            "LOOM_ANTHROPIC_BASE_URL": "https://api.anthropic.com/",
        },
    ],
)
def test_an_exchange_needs_both_roots_at_the_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str]
) -> None:
    token = tmp_path / "token"
    token.write_text("t")
    base = {
        "LOOM_INFERENCE_TOKEN_URL": "https://gateway.example/oauth/token",
        "LOOM_INFERENCE_WORKLOAD_TOKEN_FILE": str(token),
    }
    for name, value in {**base, **env}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="at the gateway"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, content=b'{"access_token": "t", "expires_in": NaN}'),
        httpx.Response(200, json={"access_token": "t", "expires_in": "Infinity"}),
        httpx.Response(200, json={"access_token": None, "expires_in": 900}),
    ],
)
def test_a_malformed_token_answer_is_no_token(gw: _Gateway, monkeypatch, answer) -> None:
    async def post(url: str, **_: Any) -> httpx.Response:
        return answer

    monkeypatch.setattr(gw, "post", post)
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))


def test_a_gateway_error_page_is_logged_with_its_status(gw: _Gateway, monkeypatch, caplog) -> None:
    async def post(url: str, **_: Any) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    monkeypatch.setattr(gw, "post", post)
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))
    assert "502" in caplog.text


def test_a_long_lived_gateway_token_is_kept_a_day_at_most(gw: _Gateway, monkeypatch) -> None:
    gw.life = 7 * 86400
    now = [1000.0]
    monkeypatch.setattr(gateway.time, "time", lambda: now[0])
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"
    assert gateway._expires_at == 1000.0 + 86400


def test_the_workload_token_follows_no_redirect(gw: _Gateway, monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    async def post(url: str, **kw: Any) -> httpx.Response:
        sent.append(kw)
        return httpx.Response(307, headers={"location": "http://elsewhere.example/oauth/token"})

    monkeypatch.setattr(gw, "post", post)
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))
    assert sent and sent[0]["follow_redirects"] is False


def test_a_retry_pause_never_keeps_an_expired_token_in_use(gw: _Gateway, monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(gateway.time, "time", lambda: now[0])
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"  # lives 900 s
    now[0] = 1000 + 895  # five seconds before its end, the gateway away
    gw.down = True
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"
    now[0] = 1000 + 901  # past its end, inside the 30 s retry pause
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))


def test_the_documented_gateway_block_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # docs/getting-started/models-and-keys.md, "Through an inference gateway".
    doc = (Path(__file__).parents[2] / "docs/getting-started/models-and-keys.md").read_text()
    block = doc.split("LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token", 1)[1]
    block = (
        "LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token" + block.split("```", 1)[0]
    )
    token = tmp_path / "token"
    token.write_text("t")
    for line in block.splitlines():
        name, _, value = line.split("#", 1)[0].strip().partition("=")
        if name:
            monkeypatch.setenv(name, value.strip().strip('"'))
    monkeypatch.setenv("LOOM_INFERENCE_WORKLOAD_TOKEN_FILE", str(token))
    Settings(_env_file=None)  # type: ignore[call-arg]
