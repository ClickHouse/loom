"""Unit tests for the Anthropic LLM client + provider selection.

Network is monkey-patched out: a stub stands in for the shared httpx
client and records the payload, so the tests pin the wire contract
(Messages API shape, JSON-mode directive + fence stripping, model-gated
temperature omission, usage mapping) without a key.
"""

from __future__ import annotations

import httpx
import pytest

import server.llm.anthropic as anthropic_mod
from server.llm import traits
from server.llm.anthropic import AnthropicLLMClient
from server.llm.client import _build_llm_client
from server.llm.fake import FakeLLMClient


class _Response:
    def __init__(self, data: dict, status: int = 200) -> None:
        self._data = data
        self.status_code = status
        self.text = str(data)
        self.request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("refused", request=self.request, response=self)  # type: ignore[arg-type]

    def json(self) -> dict:
        return self._data


class _StubHTTP:
    """Stands in for the shared httpx.AsyncClient; records each post."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.calls: list[dict] = []

    async def post(self, url: str, *, headers: dict, json: dict, **kwargs: object) -> _Response:
        # **kwargs swallows httpx-only params (timeout) the client passes.
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _Response(self._data)


def _canned(text: str = "hello", tokens=(12, 7)) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _fresh_traits() -> None:
    traits.reset_for_tests()


@pytest.mark.asyncio
async def test_complete_payload_shape(monkeypatch: pytest.MonkeyPatch, keyed) -> None:
    stub = _StubHTTP(_canned())
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)
    client = AnthropicLLMClient(model="claude-haiku-4-5")

    result = await client.complete("be terse", "say hello", max_tokens=256)

    call = stub.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "test-key"
    assert "Authorization" not in call["headers"]  # Anthropic itself takes only x-api-key
    assert call["headers"]["anthropic-version"] == anthropic_mod._API_VERSION
    body = call["json"]
    assert body["model"] == "claude-haiku-4-5"
    # system is sent as a block with a cache breakpoint: the static prefix of
    # every loom call, served from Anthropic's prompt cache on the next call
    assert body["system"] == [
        {"type": "text", "text": "be terse", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["max_tokens"] == 256  # required by the Messages API
    assert body["temperature"] == 0.0  # deterministic default, like OpenAI's
    assert body["messages"] == [{"role": "user", "content": "say hello"}]
    assert result.text == "hello"
    assert (result.tokens_in, result.tokens_out) == (12, 7)
    assert (result.provider, result.model) == ("anthropic", "claude-haiku-4-5")


@pytest.mark.asyncio
async def test_json_mode_uses_directive_not_prefill(monkeypatch: pytest.MonkeyPatch, keyed) -> None:
    """{"type": "json_object"} has no Messages-API parameter; the client
    appends a JSON-only directive to the system prompt and strips any
    markdown fences from the reply. Assistant-prefill 400s on current
    non-Haiku models, so there must be NO prefill turn."""
    stub = _StubHTTP(_canned(text='```json\n{"facts": []}\n```'))
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)
    client = AnthropicLLMClient(model="claude-haiku-4-5")

    result = await client.complete("extract", "msgs", response_format={"type": "json_object"})

    body = stub.calls[0]["json"]
    assert body["messages"] == [{"role": "user", "content": "msgs"}]
    system_text = body["system"][0]["text"]  # one cached text block
    assert system_text.startswith("extract")
    assert anthropic_mod._JSON_DIRECTIVE in system_text
    assert result.text == '{"facts": []}'


@pytest.mark.asyncio
async def test_json_mode_with_empty_system_is_directive_only(
    monkeypatch: pytest.MonkeyPatch, keyed
) -> None:
    stub = _StubHTTP(_canned(text="{}"))
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)
    client = AnthropicLLMClient(model="claude-haiku-4-5")

    await client.complete("", "msgs", response_format={"type": "json_object"})

    assert stub.calls[0]["json"]["system"][0]["text"] == anthropic_mod._JSON_DIRECTIVE


@pytest.mark.asyncio
async def test_a_refused_temperature_is_dropped_and_never_sent_again(
    monkeypatch: pytest.MonkeyPatch, keyed
) -> None:
    refusal = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "`temperature` is deprecated for this model.",
        },
    }
    stub = _SeqHTTP([(refusal, 400), _canned(), _canned()])
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)
    client = AnthropicLLMClient(model="claude-new")

    assert (await client.complete("sys", "user")).text == "hello"
    await client.complete("sys", "user")

    assert "temperature" in stub.calls[0]["json"]
    assert "temperature" not in stub.calls[1]["json"]  # the re-send
    assert "temperature" not in stub.calls[2]["json"]  # the next call, from the start
    assert len(stub.calls) == 3


@pytest.mark.asyncio
async def test_empty_system_is_omitted(monkeypatch: pytest.MonkeyPatch, keyed) -> None:
    stub = _StubHTTP(_canned())
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)
    client = AnthropicLLMClient(model="claude-haiku-4-5")

    await client.complete("", "just a user turn")

    assert "system" not in stub.calls[0]["json"]


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicLLMClient(model="claude-haiku-4-5")


def test_factory_builds_anthropic_when_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = _build_llm_client("anthropic", "claude-haiku-4-5", fake_label="fake-echo-1", warn="w")
    assert isinstance(client, AnthropicLLMClient)


def test_factory_falls_back_to_fake_when_keyless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyless boot must not crash the OSS dev container — same contract
    as the openai branch."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _build_llm_client("anthropic", "claude-haiku-4-5", fake_label="fake-echo-1", warn="w")
    assert isinstance(client, FakeLLMClient)


def test_provider_inferred_from_override_model() -> None:
    """A stage model-override routes by model name, so opus synthesis can run
    on Anthropic while the global provider stays OpenAI (and vice-versa)."""
    from server.core.llm_provider import provider_for_model

    assert provider_for_model("claude-opus-4-8", "openai") == "anthropic"
    assert provider_for_model("claude-opus-5", "openai") == "anthropic"
    assert provider_for_model("gpt-4o", "anthropic") == "openai"
    assert provider_for_model("o3-mini", "anthropic") == "openai"
    assert provider_for_model("some-local-model", "openai") == "openai"  # default


@pytest.mark.asyncio
async def test_cached_prefix_tokens_are_folded_into_input_and_reported(
    monkeypatch: pytest.MonkeyPatch, keyed
) -> None:
    """Anthropic reports cached prefix tokens OUTSIDE input_tokens. The
    client folds cache reads and writes into the billed input total and
    reports the read subset as cached_input_tokens, so cost attribution and
    the prompt_cache_hit_ratio gauge see the OpenAI-shaped (total, cached)."""
    data = _canned("hi", tokens=(100, 5))
    data["usage"].update({"cache_read_input_tokens": 900, "cache_creation_input_tokens": 0})
    http = _StubHTTP(data)
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: http)
    recorded: dict = {}

    class _Gen:
        def record_usage(self, tokens_in, tokens_out, *, priced, cached_input_tokens=0, **cache):
            recorded.update(tokens_in=tokens_in, tokens_out=tokens_out, cached=cached_input_tokens)

        def record_content(self, **kw):
            pass

        def note_attempt(self):
            pass

    import contextlib

    @contextlib.contextmanager
    def _span(**kw):
        yield _Gen()

    monkeypatch.setattr("server.observability.genai.generation_span", _span)
    client = anthropic_mod.AnthropicLLMClient(model="claude-haiku-4-5")
    result = await client.complete("be terse", "say hello", max_tokens=256)
    assert result.tokens_in == 1000, "uncached 100 + cache read 900"
    assert recorded["tokens_in"] == 1000 and recorded["cached"] == 900


class _FailingHTTP:
    async def post(self, url: str, **kwargs: object) -> None:
        raise httpx.ConnectError("down")


@pytest.mark.asyncio
async def test_outcomes_reach_provider_health(monkeypatch: pytest.MonkeyPatch, keyed) -> None:
    from server.observability import provider_health

    provider_health.reset()
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: _StubHTTP(_canned()))
    await AnthropicLLMClient("claude-haiku-4-5").complete("s", "u")
    assert provider_health.status()["providers"]["anthropic:messages"]["failing"] is False

    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: _FailingHTTP())
    monkeypatch.setattr("server.clients._retry.wait_exponential", lambda **kw: lambda rs: 0)
    with pytest.raises(httpx.ConnectError):
        await AnthropicLLMClient("claude-haiku-4-5").complete("s", "u")
    assert provider_health.status()["degraded"] is True
    provider_health.reset()


class _SeqHTTP(_StubHTTP):
    """Answers each post with the next canned reply (a body, or (body, status))."""

    def __init__(self, replies: list) -> None:
        super().__init__({})
        self._replies = list(replies)

    async def post(self, url: str, *, headers: dict, json: dict, **kwargs: object) -> _Response:
        self.calls.append({"url": url, "headers": headers, "json": dict(json)})
        reply = self._replies[min(len(self.calls), len(self._replies)) - 1]
        body, status = reply if isinstance(reply, tuple) else (reply, 200)
        return _Response(body, status)


def _thought(text: str, thinking: int, stop: str = "end_turn") -> dict:
    content = [{"type": "thinking", "thinking": "", "signature": "x"}]
    if text:
        content.append({"type": "text", "text": text})
    return {
        "stop_reason": stop,
        "content": content,
        "usage": {
            "input_tokens": 50,
            "output_tokens": thinking + len(text),
            "output_tokens_details": {"thinking_tokens": thinking},
        },
    }


@pytest.mark.asyncio
async def test_thinking_a_model_reports_becomes_its_reserve_over_the_callers_cap(
    monkeypatch: pytest.MonkeyPatch, keyed
) -> None:
    stub = _SeqHTTP([_thought("yes", 120), _thought("yes", 30)])
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)
    client = AnthropicLLMClient(model="claude-thinker")

    await client.complete("", "judge", max_tokens=200)
    await client.complete("", "judge", max_tokens=10)

    assert stub.calls[0]["json"]["max_tokens"] == 200  # nothing learned yet
    assert stub.calls[1]["json"]["max_tokens"] == 10 + 2 * 120


@pytest.mark.asyncio
async def test_a_reply_starved_by_thinking_is_sent_once_more_with_room(
    monkeypatch: pytest.MonkeyPatch, keyed
) -> None:
    stub = _SeqHTTP([_thought("", 10, stop="max_tokens"), _thought("yes", 60)])
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)

    result = await AnthropicLLMClient(model="claude-thinker").complete("", "judge", max_tokens=10)

    assert result.text == "yes"
    assert len(stub.calls) == 2
    from server.settings import settings

    assert stub.calls[1]["json"]["max_tokens"] == 10 + settings.llm_hidden_reasoning_reserve_max


@pytest.mark.asyncio
async def test_the_reserve_is_capped_by_the_setting(monkeypatch: pytest.MonkeyPatch, keyed) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_hidden_reasoning_reserve_max", 500)
    stub = _SeqHTTP([_thought("yes", 9000), _thought("yes", 1)])
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)
    client = AnthropicLLMClient(model="claude-thinker")

    await client.complete("", "q", max_tokens=10000)
    await client.complete("", "q", max_tokens=10)

    assert stub.calls[1]["json"]["max_tokens"] == 10 + 500


@pytest.mark.asyncio
async def test_effort_is_sent_when_set_and_dropped_where_refused(
    monkeypatch: pytest.MonkeyPatch, keyed
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_anthropic_effort", "low")
    refusal = {
        "type": "error",
        "error": {"message": "output_config: Extra inputs are not permitted"},
    }
    stub = _SeqHTTP([_canned("a"), (refusal, 400), _canned("b")])
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)

    await AnthropicLLMClient(model="claude-a").complete("", "q", max_tokens=10)
    await AnthropicLLMClient(model="claude-b").complete("", "q", max_tokens=10)

    assert stub.calls[0]["json"]["output_config"] == {"effort": "low"}
    assert "output_config" in stub.calls[1]["json"]
    assert "output_config" not in stub.calls[2]["json"]


@pytest.mark.asyncio
async def test_a_cap_over_the_models_limit_is_clamped_to_the_limit_it_names(
    monkeypatch: pytest.MonkeyPatch, keyed
) -> None:
    refusal = {
        "type": "error",
        "error": {
            "message": "max_tokens: 20000 > 8192, which is the maximum allowed number of "
            "output tokens for claude-small"
        },
    }
    stub = _SeqHTTP([(refusal, 400), _canned(), _canned()])
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: stub)
    client = AnthropicLLMClient(model="claude-small")

    await client.complete("", "q", max_tokens=20000)
    await client.complete("", "q", max_tokens=30000)

    assert [c["json"]["max_tokens"] for c in stub.calls] == [20000, 8192, 8192]
