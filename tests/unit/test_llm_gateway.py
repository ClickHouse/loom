"""Loom's own model calls through an inference gateway: API roots that point
at it, and a workload token exchanged there instead of a provider key."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.clients import _http
from server.core.errors import GatewayRequiredError
from server.llm import gateway
from server.llm.customer import Customer
from server.memory.app.log_once import LogOnce
from server.settings import Settings, settings
from tests.unit.model_client import through_the_model_client

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
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "inference_workload_token_file", str(token))
    monkeypatch.setattr(settings, "inference_token_scope", "chat embeddings")
    fake = _Gateway()
    monkeypatch.setattr("server.clients._http.get_model_client", lambda: fake)
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
    asyncio.run(embedder._post(embedder._payload("text"), Customer()))
    assert vectors.calls[0]["url"] == "https://gateway.example/v1/embeddings"
    assert vectors.calls[0]["headers"]["Authorization"] == "Bearer gw-1"


def test_anthropic_behind_a_gateway_also_gets_the_key_as_a_bearer(monkeypatch) -> None:
    from server.llm import anthropic as anthropic_mod

    monkeypatch.setattr(settings, "anthropic_base_url", "https://gateway.example")
    recorder = _Recorder({})
    asyncio.run(anthropic_mod._post(recorder, "claude-haiku-4-5", "svc-key", {}, Customer()))
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
        "LOOM_INFERENCE_GATEWAY_HOSTS": "gateway.example",
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
        "https://myres.openai.azure.com/openai/deployments/d/chat/completions",
        "https://10.0.0.5/v1/chat/completions",  # an IP literal
    ],
)
def test_the_gateway_token_never_goes_off_the_gateways_hosts(gw: _Gateway, url: str) -> None:
    # Whatever path picked the host: a model named per request, the dashboard's check.
    with pytest.raises(GatewayRequiredError, match="not in LOOM_INFERENCE_GATEWAY_HOSTS"):
        asyncio.run(gateway.bearer("", url))
    assert gw.sent == []


def test_a_claude_model_named_per_request_does_not_take_the_token_to_anthropic(
    gw: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.llm import anthropic as anthropic_mod

    recorder = _Recorder({})
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: recorder)
    client = anthropic_mod.AnthropicLLMClient(model="claude-haiku-4-5", api_key="")
    with pytest.raises(GatewayRequiredError):
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
        "LOOM_INFERENCE_GATEWAY_HOSTS": "gateway.example",
    }
    for name, value in {**base, **env}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="at a host in LOOM_INFERENCE_GATEWAY_HOSTS"):
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


def _full_log_once() -> LogOnce:
    """A LogOnce that has logged as many keys as it remembers."""
    full = LogOnce()
    assert all(full.first(("acme", f"ns-{i}")) for i in range(1000))
    return full


def test_a_log_once_logs_each_key_once_up_to_its_cap() -> None:
    once = LogOnce()
    assert once.first("a") and not once.first("a")
    full = _full_log_once()
    assert not full.first("new")  # past the cap
    assert not full.first(("acme", "ns-0"))


_AT_GATEWAY = {
    "LOOM_OPENAI_BASE_URL": "https://gateway.example/v1",
    "LOOM_ANTHROPIC_BASE_URL": "https://gateway.example",
    "LOOM_INFERENCE_GATEWAY_HOSTS": "gateway.example",
}

# Hosts that are no gateway of this deployment: a provider, a cloud's own
# endpoint, an IP literal, a name that only starts or ends like the gateway's.
_OFF = [
    "https://api.openai.com/v1/chat/completions",
    "https://api.anthropic.com",
    "https://myres.openai.azure.com/openai/deployments/d/embeddings",
    "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke",
    "https://10.0.0.5/v1/chat/completions",
    "https://[::1]/v1/embeddings",
    "https://gateway.example.attacker.test/v1",
    "https://attacker.test/gateway.example/v1",
    "https://api.cohere.com/v2/rerank",
]


@pytest.fixture
def required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A static gateway key, with the gateway required at gateway.example."""
    monkeypatch.setattr(settings, "inference_gateway_required", True)
    monkeypatch.setattr(settings, "inference_gateway_hosts", "Gateway.Example")
    monkeypatch.setattr(settings, "inference_token_url", "")


@pytest.mark.parametrize("url", _OFF)
def test_with_the_gateway_required_a_key_never_goes_off_its_hosts(required: None, url: str) -> None:
    with pytest.raises(GatewayRequiredError, match="not in LOOM_INFERENCE_GATEWAY_HOSTS"):
        asyncio.run(gateway.bearer("sk-gateway", url))


@pytest.mark.parametrize("url", [_GW, "https://GATEWAY.example./v1/embeddings"])
def test_with_the_gateway_required_its_key_is_the_bearer_at_its_hosts(
    required: None, url: str
) -> None:
    assert asyncio.run(gateway.bearer("sk-gateway", url)) == "sk-gateway"


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The model client, its hook in place, over a transport that records each
    URL it is sent and redirects the gateway's /moved to a host it does not list."""
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        if request.url.path.endswith("/moved"):
            return httpx.Response(307, headers={"location": "https://mirror.example/v1/embeddings"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5] * 8}]})

    monkeypatch.setattr(_http, "_model_client", None)  # restored after the test
    client = _http.get_model_client()
    monkeypatch.setattr(client, "_transport", httpx.MockTransport(answer))
    yield sent
    asyncio.run(client.aclose())


def test_with_the_gateway_required_no_redirect_takes_a_call_off_its_hosts(
    required: None, wire: list[str]
) -> None:
    async def follow() -> None:
        await _http.get_model_client().post("https://gateway.example/v1/moved", json={})

    with pytest.raises(GatewayRequiredError):
        asyncio.run(follow())
    assert wire == ["https://gateway.example/v1/moved"]  # the other host's hop never left


def test_only_the_model_client_refuses_a_host_off_the_gateway(
    required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GitHub and an issuer's discovery document are not model calls.
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json={})

    monkeypatch.setattr(_http, "_client", None)
    monkeypatch.setattr(_http, "_model_client", None)
    other, model = _http.get_client(), _http.get_model_client()
    for client in (other, model):
        monkeypatch.setattr(client, "_transport", httpx.MockTransport(answer))

    async def send(client: httpx.AsyncClient) -> None:
        await client.get("https://api.github.com/repos")

    asyncio.run(send(other))
    with pytest.raises(GatewayRequiredError):
        asyncio.run(send(model))
    assert sent == ["https://api.github.com/repos"]
    for client in (other, model):
        asyncio.run(client.aclose())


@pytest.mark.parametrize("url", _OFF[:7])
def test_with_the_gateway_required_no_embedder_posts_off_its_hosts(
    required: None, wire: list[str], monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    # Neither calls bearer: the model client refuses the host, at once.
    from server.embedding import compatible, gemini

    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    direct = compatible.CompatibleEmbedder(
        model="text-embedding-3-small", dim=8, base_url=url, api_key="k"
    )
    with pytest.raises(GatewayRequiredError):
        asyncio.run(direct._request(["text"]))
    google = gemini.GeminiEmbedder(model="gemini-embedding-001", dim=8)
    with pytest.raises(GatewayRequiredError):
        asyncio.run(google._post(google._endpoint, google._content_request("text"), Customer()))
    assert wire == []
    assert not issubclass(GatewayRequiredError, httpx.TransportError)  # never retried


def test_the_dashboards_check_names_the_refusal(required: None, wire: list[str]) -> None:
    from server.insights.app import provider_validation

    check = asyncio.run(
        provider_validation._check(
            "embedding",
            "compatible",
            "text-embedding-3-small",
            "k",
            8,
            base_url="https://myres.openai.azure.com/openai",
        )
    )
    assert (check.ready, check.code) == (False, "gateway_required")


class _Posts:
    """The model client's wire: records each URL posted to, and answers `answer`."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.posted: list[str] = []
        self.answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posted.append(str(request.url))
        return httpx.Response(200, json=self.answer)


def _posts(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any] | None = None) -> _Posts:
    wire = _Posts(answer or {})
    client = through_the_model_client(monkeypatch)
    monkeypatch.setattr(client, "_transport", httpx.MockTransport(wire))
    return wire


def test_no_rerank_or_classify_endpoint_posts_off_the_gateways_hosts(
    required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each posts on the model client, whose hook refuses the URL on every hop.
    from server.memory.app import classify, rerank
    from server.memory.app.rerank import RerankCandidate

    wire = _posts(monkeypatch)
    monkeypatch.setenv("COHERE_API_KEY", "c-key")
    monkeypatch.setattr(settings, "rerank_endpoints_json", '{"direct": "https://10.0.0.5/rerank"}')
    candidates = [RerankCandidate("k", "ns", "text", "observation", 0.5)]
    for endpoint in ("direct", "builtin:cohere"):
        out = asyncio.run(
            rerank.rerank(endpoint=endpoint, prompt_template="", query="q", candidates=candidates)
        )
        assert out == candidates  # fails open, in input order
    for url in _OFF:
        label = asyncio.run(
            classify.classify(
                org="acme",
                namespace="ns",
                endpoint=url,
                prompt_template="",
                value="v",
                valid_types=[],
            )
        )
        assert label == ""  # the caller's type stays
    assert wire.posted == []


@pytest.mark.parametrize(
    "url",
    [
        "https://gateway.example@attacker.test/c",  # userinfo, then the host
        "https://a@gateway.example@attacker.test/c",  # several @
        "https://attacker.test\\@gateway.example/c",  # a backslash before @
        "https://gatewаy.example/c",  # a Cyrillic а: an IDN, punycode on the wire
        "https://xn--gatewy-6ve.example/c",
        "https://[::1]/c",
    ],
)
def test_the_gateway_check_reads_a_url_as_httpx_sends_it(required: None, url: str) -> None:
    # Checked with the parser that sends, so the two never disagree: refused
    # exactly when httpx would send somewhere other than gateway.example.
    from server.memory.domain.classify_endpoints import classify_refusal

    try:
        sent_to = httpx.URL(url).raw_host.decode("ascii")
    except httpx.InvalidURL:
        sent_to = ""
    off = sent_to != "gateway.example"
    assert bool(settings.off_gateway(url)) is off
    assert bool(classify_refusal(url)) is off


def test_an_idn_host_is_matched_as_the_punycode_httpx_sends_to(
    required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    punycode = httpx.URL("https://gatewаy.example/").raw_host.decode("ascii")
    monkeypatch.setattr(settings, "inference_gateway_hosts", punycode)
    assert settings.off_gateway("https://gatewаy.example/v1") == ""
    assert settings.off_gateway("https://gateway.example/v1") == "gateway.example"


def test_a_classify_endpoint_at_a_listed_host_receives_the_post(
    required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import classify

    wire = _posts(monkeypatch, {"memory_type": "decision"})
    label = asyncio.run(
        classify.classify(
            org="acme",
            namespace="ns",
            endpoint="https://gateway.example/classify",
            prompt_template="",
            value="v",
            valid_types=["decision"],
        )
    )
    assert label == "decision" and wire.posted == ["https://gateway.example/classify"]


def test_a_classify_endpoint_off_the_gateways_hosts_is_refused_at_write(required: None) -> None:
    from pydantic import ValidationError

    from server.identity.api.models import NamespaceRegisterInput

    base = {"org": "acme", "namespace": "ns", "owner_email": "a@acme.test"}
    for url in (
        "https://myres.openai.azure.com/classify",
        "http://169.254.169.254/latest",
        "http://gateway.example/classify",  # a listed host, but plain http
        "https://gateway.example:8080/x",  # a listed host, another port
    ):
        with pytest.raises(ValidationError, match="classify_endpoint"):
            NamespaceRegisterInput(**base, classify_endpoint=url)
    ok = NamespaceRegisterInput(**base, classify_endpoint="https://gateway.example/classify")
    assert ok.classify_endpoint == "https://gateway.example/classify"


def test_a_stored_classify_endpoint_off_the_gateways_hosts_is_never_called(
    required: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.core import ch
    from server.memory.app import classify

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, str]:
            return {"classify_endpoint": "https://10.0.0.5/c", "classify_prompt_template": "p"}

    assert classify.classify_refusal("https://gateway.example:8443/c")  # stored, another port

    monkeypatch.setattr(ch, "get_client", lambda: _CH())
    monkeypatch.setattr(classify, "_REFUSED_LOGGED", LogOnce())
    for _ in range(2):
        assert asyncio.run(classify.get_namespace_classify_config("acme", "ns")) == ("", "")
    warned = [r for r in caplog.records if "classify_endpoint is refused" in r.getMessage()]
    assert len(warned) == 1 and "10.0.0.5" in warned[0].getMessage()
    caplog.clear()
    monkeypatch.setattr(classify, "_REFUSED_LOGGED", _full_log_once())
    assert asyncio.run(classify.get_namespace_classify_config("acme", "new")) == ("", "")
    assert not caplog.records  # capped: still refused, not logged
    monkeypatch.setattr(settings, "inference_gateway_required", False)
    config = asyncio.run(classify.get_namespace_classify_config("acme", "ns"))
    assert config == ("https://10.0.0.5/c", "p")


@pytest.fixture
def roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both API roots at the gateway, so only a configuration can be refused."""
    monkeypatch.setattr(settings, "openai_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "anthropic_base_url", "https://gateway.example")


def _setup(provider: str = "compatible", base_url: str = "") -> Any:
    from server.insights.api.models_provider_setup import ProviderSetupInput

    model = "gemini-embedding-001" if provider == "gemini" else "text-embedding-3-small"
    return ProviderSetupInput.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-6-luna"},
            "embedding": {"provider": provider, "model": model, "base_url": base_url},
        }
    )


def test_a_dashboard_save_off_the_gateway_answers_422(required: None, roots: None) -> None:
    from fastapi import HTTPException

    from server.insights.app import provider_activation

    with pytest.raises(HTTPException) as refused:
        asyncio.run(provider_activation._save_locked(_setup("compatible", "https://10.0.0.5/v1")))
    assert refused.value.status_code == 422
    assert "LOOM_INFERENCE_GATEWAY_HOSTS" in str(refused.value.detail)


@pytest.mark.parametrize("rerank", ["builtin:cohere", "builtin:cohere:rerank-english-v3.0"])
@pytest.mark.parametrize(
    "setting", ["LOOM_CODE_SEARCH_RERANK_DEFAULT", "LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT"]
)
def test_the_gateway_required_refuses_cohere_as_a_search_rerank_default(
    monkeypatch: pytest.MonkeyPatch, rerank: str, setting: str
) -> None:
    env = _AT_GATEWAY | {"LOOM_INFERENCE_GATEWAY_REQUIRED": "true", setting: rerank}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=setting):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv(setting, "builtin:openai")
    assert Settings(_env_file=None).model_dump()[setting.removeprefix("LOOM_").lower()] == (  # type: ignore[call-arg]
        "builtin:openai"
    )


def test_a_namespace_that_stored_builtin_cohere_reranks_with_nothing(
    required: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Stored before the gateway was required: unset, not refused at each search.
    from server.core import ch
    from server.memory.app import rerank

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, str]:
            return {"rerank_endpoint": "builtin:cohere", "rerank_prompt_template": "p"}

    monkeypatch.setattr(ch, "get_client", lambda: _CH())
    monkeypatch.setattr(rerank, "_COHERE_UNSET_LOGGED", LogOnce())
    for _ in range(2):
        assert asyncio.run(rerank.get_namespace_rerank_config("acme", "ns")) == ("", "")
    warned = [r for r in caplog.records if "builtin:cohere" in r.getMessage()]
    assert len(warned) == 1 and "LOOM_INFERENCE_GATEWAY_REQUIRED" in warned[0].getMessage()
    monkeypatch.setattr(settings, "inference_gateway_required", False)
    config = asyncio.run(rerank.get_namespace_rerank_config("acme", "ns"))
    assert config == ("builtin:cohere", "p")


def test_past_its_log_cap_a_stored_builtin_cohere_still_gets_no_reranker(
    required: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.core import ch
    from server.memory.app import rerank

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, str]:
            return {"rerank_endpoint": "builtin:cohere", "rerank_prompt_template": ""}

    monkeypatch.setattr(ch, "get_client", lambda: _CH())
    monkeypatch.setattr(rerank, "_COHERE_UNSET_LOGGED", _full_log_once())
    assert asyncio.run(rerank.get_namespace_rerank_config("acme", "new")) == ("", "")
    assert not [r for r in caplog.records if "builtin:cohere" in r.getMessage()]


def test_a_refused_cohere_names_the_setting_that_requires_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.domain.rerank_names import check_rerank_name

    monkeypatch.setattr(settings, "inference_gateway_required", False)
    monkeypatch.setattr(settings, "inference_token_url", "https://gateway.example/oauth/token")
    with pytest.raises(ValueError, match="while LOOM_INFERENCE_TOKEN_URL is set"):
        check_rerank_name("builtin:cohere")


def test_with_the_gateway_required_builtin_cohere_is_refused_by_name(required: None) -> None:
    from server.memory.domain.rerank_names import check_rerank_name

    for name in ("builtin:cohere", "builtin:cohere:rerank-english-v3.0"):
        with pytest.raises(ValueError, match="LOOM_INFERENCE_GATEWAY_REQUIRED"):
            check_rerank_name(name)
    assert check_rerank_name("builtin:openai") == "builtin:openai"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LOOM_INFERENCE_GATEWAY_HOSTS": ""}, "needs LOOM_INFERENCE_GATEWAY_HOSTS"),
        ({"LOOM_OPENAI_BASE_URL": "https://api.openai.com/v1"}, "LOOM_OPENAI_BASE_URL"),
        (
            {"LOOM_OPENAI_BASE_URL": "https://myres.openai.azure.com/openai"},
            "LOOM_OPENAI_BASE_URL at a host in LOOM_INFERENCE_GATEWAY_HOSTS, not myres",
        ),
        ({"LOOM_ANTHROPIC_BASE_URL": "https://10.0.0.5"}, "LOOM_ANTHROPIC_BASE_URL"),
        (
            {"LOOM_ANTHROPIC_BASE_URL": "https://gateway.example.attacker.test"},
            "LOOM_ANTHROPIC_BASE_URL",
        ),
        (
            {
                "LOOM_EMBEDDING_PROVIDER": "compatible",
                "LOOM_EMBEDDING_BASE_URL": "https://mirror.example/v1",
            },
            "LOOM_EMBEDDING_BASE_URL",
        ),
        ({"LOOM_EMBEDDING_MODEL": "gemini-embedding-001"}, "Gemini embedder"),
        ({"LOOM_RERANK_ENDPOINTS_JSON": '{"direct": "https://10.0.0.5/rerank"}'}, "entry 'direct'"),
    ],
)
def test_the_gateway_required_refuses_to_load_with_an_address_off_its_hosts(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for name, value in {**_AT_GATEWAY, "LOOM_INFERENCE_GATEWAY_REQUIRED": "true", **env}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"LOOM_INFERENCE_GATEWAY_REQUIRED.*{message}"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "hosts", ["*.gateway.example", "https://gateway.example", "gateway.example:443", "gate way"]
)
def test_a_gateway_host_is_a_bare_hostname(monkeypatch: pytest.MonkeyPatch, hosts: str) -> None:
    monkeypatch.setenv("LOOM_INFERENCE_GATEWAY_HOSTS", hosts)
    with pytest.raises(ValueError, match="is not a hostname"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_gateway_required_loads_with_every_root_at_its_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _AT_GATEWAY | {
        "LOOM_INFERENCE_GATEWAY_REQUIRED": "true",
        "LOOM_INFERENCE_GATEWAY_HOSTS": "gateway.example, embed.gateway.example",
        "LOOM_EMBEDDING_PROVIDER": "compatible",
        "LOOM_EMBEDDING_BASE_URL": "https://EMBED.gateway.example/v1",
        "LOOM_RERANK_ENDPOINTS_JSON": '{"ranker": "https://gateway.example/rerank"}',
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]
    assert loaded.gateway_required()
    assert loaded.gateway_hosts() == {"gateway.example", "embed.gateway.example"}


def test_an_exchange_needs_its_token_endpoint_at_the_gateways_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "token"
    token.write_text("t")
    env = _AT_GATEWAY | {
        "LOOM_INFERENCE_TOKEN_URL": "https://idp.example/oauth/token",
        "LOOM_INFERENCE_WORKLOAD_TOKEN_FILE": str(token),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(
        ValueError, match="does not list idp.example, the host of LOOM_INFERENCE_TOKEN_URL"
    ):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_an_exchange_requires_the_gateway_and_refuses_the_gemini_embedder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "token"
    token.write_text("t")
    env = _AT_GATEWAY | {
        "LOOM_INFERENCE_TOKEN_URL": "https://gateway.example/oauth/token",
        "LOOM_INFERENCE_WORKLOAD_TOKEN_FILE": str(token),
        "LOOM_EMBEDDING_PROVIDER": "gemini",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="LOOM_INFERENCE_TOKEN_URL refuses the Gemini embedder"):
        Settings(_env_file=None)  # type: ignore[call-arg]
