"""A keyed LLM provider without its key runs on the fake client, and says so.

The OSS dev container boots keyless. `_build_llm_client` answers with
`FakeLLMClient` and logs one warning rather than raising at the first
extraction. That warning is the only sign that facts will not be extracted.
`provider_key_missing` is what `loom doctor` and the degraded banner read.
Both selectors (the default client and a stage's model override) share the
branch.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from server.llm import client as llm_client
from server.llm.fake import FakeLLMClient
from server.llm.fallback import FallbackLLMClient
from server.settings import settings

KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


@pytest.fixture(autouse=True)
def _fresh_client_cache() -> Iterator[None]:
    llm_client.reset()
    yield
    llm_client.reset()


@pytest.mark.parametrize("provider", sorted(KEYS))
def test_a_keyless_provider_builds_the_fake_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, provider: str
) -> None:
    monkeypatch.delenv(KEYS[provider], raising=False)
    caplog.set_level(logging.WARNING, logger="server.llm.client")
    built = llm_client._build_llm_client(provider, "m", fake_label="fake-echo-1", warn="no key")
    assert isinstance(built, FakeLLMClient) and built.model == "fake-echo-1"
    assert [r.getMessage() for r in caplog.records] == ["no key"]
    assert llm_client.provider_key_missing(provider) is True


@pytest.mark.parametrize("provider", ["fake", "unknown"])
def test_only_keyed_providers_can_miss_a_key(provider: str) -> None:
    assert llm_client.provider_key_missing(provider) is False


def test_an_unknown_provider_is_an_error_not_a_silent_fake() -> None:
    with pytest.raises(ValueError, match="unknown llm provider"):
        llm_client._build_llm_client("mystery", "m", fake_label="f", warn="w")


def test_the_default_client_is_the_fake_under_the_resolved_provider(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "llm_fallback_model", "")
    caplog.set_level(logging.WARNING, logger="server.llm.client")
    built = llm_client.get_llm_client()
    assert isinstance(built, FakeLLMClient) and built.model == "fake-echo-1"
    assert "LOOM_LLM_PROVIDER=openai but its API key is not set" in caplog.text
    assert llm_client.get_llm_client() is built, "the choice is made once per process"


def test_a_stage_override_on_a_keyless_provider_is_a_labelled_fake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "llm_fallback_model", "")
    built = llm_client.get_llm_client_for_model("claude-haiku-4-5")
    assert isinstance(built, FakeLLMClient)
    # The label names the model the stage asked for, so a trace shows what did not run.
    assert built.model == "fake-echo-1[claude-haiku-4-5]"


def test_a_fallback_model_wraps_the_keyless_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "llm_fallback_model", "fake-echo-2")
    built = llm_client.get_llm_client()
    assert isinstance(built, FallbackLLMClient)
    assert isinstance(built.primary, FakeLLMClient)
    assert isinstance(built.secondary, FakeLLMClient) and built.secondary.model == "fake-echo-2"
