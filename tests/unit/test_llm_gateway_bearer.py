"""The workload token as the bearer itself (LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer):
read from its file at most once a minute and again at once after a 401, sent with
every model call to the gateway; no exchange and no provider key."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.llm import gateway
from server.settings import Settings, settings
from tests.unit.clock_fake import ManualClock
from tests.unit.model_client import through_the_model_client

_CHAT = {
    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}
_MESSAGES = {
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 1, "output_tokens": 1},
    "stop_reason": "end_turn",
}
_VECTORS = {"data": [{"index": 0, "embedding": [0.5] * 8}], "usage": {"total_tokens": 1}}


@pytest.fixture
def token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Bearer mode, the gateway required at gateway.example, every root there, no keys."""
    path = tmp_path / "token"
    path.write_text("sa-token-1\n")
    monkeypatch.setattr(settings, "inference_workload_token_mode", "bearer")
    monkeypatch.setattr(settings, "inference_workload_token_file", str(path))
    monkeypatch.setattr(settings, "inference_token_url", "")
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "openai_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "anthropic_base_url", "https://gateway.example")
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LOOM_EMBEDDING_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    gateway._reset_for_tests()
    yield path
    gateway._reset_for_tests()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    c = ManualClock(t=1000.0)
    monkeypatch.setattr(gateway.time, "monotonic", c.monotonic)
    return c


class _Wire:
    """The model client, its hooks in place, over a transport that records each
    request's bearer and answers 401 to the ones in `refused`."""

    def __init__(self) -> None:
        self.bearers: list[str] = []
        self.refused: set[str] = set()

    def answer(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("authorization", "")
        self.bearers.append(bearer)
        if bearer in self.refused:
            return httpx.Response(401, json={"error": {"code": "invalid_token"}})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json=_CHAT)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=_MESSAGES)
        return httpx.Response(200, json=_VECTORS)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Wire]:
    w = _Wire()
    client = through_the_model_client(monkeypatch)
    monkeypatch.setattr(client, "_transport", httpx.MockTransport(w.answer))
    yield w
    asyncio.run(client.aclose())


def _chat() -> Any:
    from server.llm.openai import OpenAILLMClient

    return asyncio.run(OpenAILLMClient(model="gpt-4o-mini").complete("s", "u"))


def test_the_files_content_is_the_bearer_of_every_model_call(token_file, wire) -> None:
    from server.embedding.compatible import CompatibleEmbedder
    from server.embedding.openai import OpenAIEmbedder
    from server.llm.anthropic import AnthropicLLMClient

    assert gateway.keyless() and not gateway.exchanging()
    _chat()
    asyncio.run(AnthropicLLMClient(model="claude-haiku-4-5").complete("s", "u"))
    asyncio.run(OpenAIEmbedder(model="text-embedding-3-small", dim=8).embed_many(["text"]))
    compatible = CompatibleEmbedder(
        model="m", dim=8, base_url="https://gateway.example/v1", api_key=""
    )
    asyncio.run(compatible.embed_many(["text"]))
    assert wire.bearers == ["Bearer sa-token-1"] * 4


def test_anthropic_gets_the_token_as_its_key_too(token_file, monkeypatch) -> None:
    from server.llm import anthropic as anthropic_mod

    sent: list[dict[str, str]] = []

    class _Recorder:
        async def post(self, url: str, **kw: Any) -> httpx.Response:
            sent.append(kw["headers"])
            return httpx.Response(200, json=_MESSAGES, request=httpx.Request("POST", url))

    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: _Recorder())
    asyncio.run(anthropic_mod.AnthropicLLMClient(model="claude-haiku-4-5").complete("s", "u"))
    assert sent[0]["Authorization"] == "Bearer sa-token-1" and sent[0]["x-api-key"] == "sa-token-1"


def test_a_compatible_endpoints_own_key_still_wins(token_file, wire) -> None:
    from server.embedding.compatible import CompatibleEmbedder

    keyed = CompatibleEmbedder(
        model="m", dim=8, base_url="https://gateway.example/v1", api_key="static-key"
    )
    asyncio.run(keyed.embed_many(["text"]))
    assert wire.bearers == ["Bearer static-key"]


def test_the_file_is_read_at_most_once_a_minute(token_file, clock) -> None:
    assert asyncio.run(gateway.bearer("", "https://gateway.example/v1")) == "sa-token-1"
    token_file.write_text("sa-token-2")  # the platform rotated it
    clock.advance(59)
    assert asyncio.run(gateway.bearer("", "https://gateway.example/v1")) == "sa-token-1"
    clock.advance(1)
    assert asyncio.run(gateway.bearer("", "https://gateway.example/v1")) == "sa-token-2"


def test_a_401_makes_the_next_call_read_the_file_again(token_file, clock, wire) -> None:
    _chat()
    token_file.write_text("sa-token-2")
    wire.refused.add("Bearer sa-token-1")  # the gateway no longer takes it
    with pytest.raises(httpx.HTTPStatusError, match="401"):
        _chat()  # not retried: the same token would be refused again
    _chat()  # no time has passed: the 401 alone made it read the file
    assert wire.bearers == ["Bearer sa-token-1", "Bearer sa-token-1", "Bearer sa-token-2"]


def test_an_unreadable_file_serves_the_token_in_hand_and_with_none_is_an_outage(
    token_file, clock
) -> None:
    url = "https://gateway.example/v1"
    token_file.unlink()
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", url))
    token_file.write_text("sa-token-1")
    clock.advance(60)  # a failed read is tried again a minute later
    assert asyncio.run(gateway.bearer("", url)) == "sa-token-1"
    token_file.unlink()
    clock.advance(60)
    assert asyncio.run(gateway.bearer("", url)) == "sa-token-1"  # the token in hand


def test_a_bearer_deployment_builds_real_providers_with_no_key(token_file, monkeypatch) -> None:
    from server.embedding import embedder
    from server.embedding.compatible import CompatibleEmbedder
    from server.llm.client import provider_key_missing
    from server.observability.problems import HealthState, collect_problems

    monkeypatch.setattr(settings, "embedding_provider", "compatible")
    monkeypatch.setattr(settings, "embedding_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "embedding_dim", 8)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    assert isinstance(embedder._served("gemini-embedding-2"), CompatibleEmbedder)
    assert not provider_key_missing("openai") and not provider_key_missing("anthropic")
    codes = {p.code for p in collect_problems(settings, HealthState(env={}))}
    assert "llm_key_missing" not in codes and "embedder_key_missing" not in codes


_BEARER = {
    "LOOM_INFERENCE_WORKLOAD_TOKEN_MODE": "Bearer",
    "LOOM_INFERENCE_WORKLOAD_TOKEN_FILE": "/var/run/secrets/inference/token",
    "LOOM_INFERENCE_GATEWAY_HOSTS": "gateway.example",
    "LOOM_OPENAI_BASE_URL": "https://gateway.example/v1",
    "LOOM_ANTHROPIC_BASE_URL": "https://gateway.example",
}


@pytest.mark.parametrize(
    ("drop", "add", "message"),
    [
        ("LOOM_INFERENCE_WORKLOAD_TOKEN_FILE", {}, "needs LOOM_INFERENCE_WORKLOAD_TOKEN_FILE"),
        (
            "",
            {"LOOM_INFERENCE_TOKEN_URL": "https://gateway.example/oauth/token"},
            "LOOM_INFERENCE_TOKEN_URL is for the exchange",
        ),
        ("LOOM_INFERENCE_GATEWAY_HOSTS", {}, "needs LOOM_INFERENCE_GATEWAY_HOSTS"),
        ("LOOM_ANTHROPIC_BASE_URL", {}, "LOOM_ANTHROPIC_BASE_URL at a host"),
    ],
)
def test_the_bearer_mode_needs_its_file_and_the_gateway_and_no_exchange(
    monkeypatch: pytest.MonkeyPatch, drop: str, add: dict[str, str], message: str
) -> None:
    for name, value in {**_BEARER, **add}.items():
        if name != drop:
            monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_documented_bearer_block_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    # docs/getting-started/models-and-keys.md, "Through an inference gateway".
    doc = (Path(__file__).parents[2] / "docs/getting-started/models-and-keys.md").read_text()
    head = "LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer"
    block = head + doc.split(head, 1)[1].split("```", 1)[0]
    for line in block.splitlines():
        name, _, value = line.split("#", 1)[0].strip().partition("=")
        if name:
            monkeypatch.setenv(name, value.strip().strip('"'))
    assert Settings(_env_file=None).gateway_required()  # type: ignore[call-arg]


def test_the_bearer_mode_loads_and_requires_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).inference_workload_token_mode == "exchange"  # type: ignore[call-arg]
    for name, value in _BEARER.items():
        monkeypatch.setenv(name, value)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.inference_workload_token_mode == "bearer" and s.gateway_required()
