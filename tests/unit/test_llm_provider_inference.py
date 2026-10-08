"""The LLM provider is inferred from the model and the key,
the way the embedder already is.

Reading `settings.llm_provider` verbatim with a `fake` default would run
extraction for `LOOM_LLM_MODEL=gpt-4o-mini` (provider unset) on the echo
client and yield zero facts. An unset provider is resolved once at Settings
load; an explicit value is never overridden.
"""

from __future__ import annotations

import logging

import pytest

from server.core.llm_provider import ResolvedLLM, resolve_llm_provider

OPENAI = {"OPENAI_API_KEY": "sk-test"}
ANTHROPIC = {"ANTHROPIC_API_KEY": "sk-ant-test"}
BOTH = {**OPENAI, **ANTHROPIC}
NONE: dict[str, str] = {}


@pytest.mark.parametrize(
    ("provider_env", "model_env", "env", "expected"),
    [
        # Explicit provider always wins, model defaults per provider.
        ("openai", "", NONE, ResolvedLLM("openai", "gpt-6-luna", "LOOM_LLM_PROVIDER")),
        ("anthropic", "", NONE, ResolvedLLM("anthropic", "claude-haiku-4-5", "LOOM_LLM_PROVIDER")),
        ("fake", "", BOTH, ResolvedLLM("fake", "fake-echo-1", "LOOM_LLM_PROVIDER")),
        ("fake", "gpt-4o-mini", OPENAI, ResolvedLLM("fake", "gpt-4o-mini", "LOOM_LLM_PROVIDER")),
        (" OpenAI ", "gpt-5", NONE, ResolvedLLM("openai", "gpt-5", "LOOM_LLM_PROVIDER")),
        # Unset provider: the model name decides, key or no key.
        (
            "",
            "gpt-4o-mini",
            NONE,
            ResolvedLLM("openai", "gpt-4o-mini", "LOOM_LLM_MODEL=gpt-4o-mini"),
        ),
        ("", "o3-mini", NONE, ResolvedLLM("openai", "o3-mini", "LOOM_LLM_MODEL=o3-mini")),
        (
            "",
            "claude-opus-4-1",
            OPENAI,
            ResolvedLLM("anthropic", "claude-opus-4-1", "LOOM_LLM_MODEL=claude-opus-4-1"),
        ),
        ("", "fake-echo-1", BOTH, ResolvedLLM("fake", "fake-echo-1", "LOOM_LLM_MODEL=fake-echo-1")),
        # Unset provider and an unknown model name: the key decides, model kept.
        ("", "mystery-7b", OPENAI, ResolvedLLM("openai", "mystery-7b", "OPENAI_API_KEY is set")),
        ("", "mystery-7b", NONE, ResolvedLLM("fake", "mystery-7b", "nothing set")),
        # Nothing but a key: provider + its default model.
        ("", "", OPENAI, ResolvedLLM("openai", "gpt-6-luna", "OPENAI_API_KEY is set")),
        (
            "",
            "",
            ANTHROPIC,
            ResolvedLLM("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY is set"),
        ),
        ("", "", BOTH, ResolvedLLM("openai", "gpt-6-luna", "OPENAI_API_KEY is set")),
        # Nothing at all: the keyless fake default the dev container relies on.
        ("", "", NONE, ResolvedLLM("fake", "fake-echo-1", "nothing set")),
        # An empty key value is "not set".
        ("", "", {"OPENAI_API_KEY": ""}, ResolvedLLM("fake", "fake-echo-1", "nothing set")),
    ],
)
def test_resolution_table(
    provider_env: str, model_env: str, env: dict[str, str], expected: ResolvedLLM
) -> None:
    assert resolve_llm_provider(provider_env, model_env, env) == expected


def test_inferred_flag_is_false_only_for_an_explicit_provider() -> None:
    assert not resolve_llm_provider("openai", "", NONE).inferred
    assert resolve_llm_provider("", "gpt-4o-mini", NONE).inferred
    assert resolve_llm_provider("", "", OPENAI).inferred
    assert resolve_llm_provider("", "", NONE).inferred


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LOOM_LLM_PROVIDER", "LOOM_LLM_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_settings_resolve_unset_provider_from_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`settings.llm_provider` is concrete after load, so the client factory,
    /v1/health and doctor all read the same resolved value."""
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = Settings(_env_file=None)
    assert (s.llm_provider, s.llm_model) == ("openai", "gpt-6-luna")
    assert s.llm_provider_inferred()
    assert s.llm_provider_source() == "OPENAI_API_KEY is set"


def test_settings_resolve_unset_provider_from_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_MODEL", "gpt-4o-mini")
    s = Settings(_env_file=None)
    assert (s.llm_provider, s.llm_model) == ("openai", "gpt-4o-mini")
    assert s.llm_provider_source() == "LOOM_LLM_MODEL=gpt-4o-mini"


def test_settings_explicit_fake_wins_over_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stock .env pins LOOM_LLM_PROVIDER=fake; a key in the shell must
    not flip it (the stock-stack test depends on this)."""
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_PROVIDER", "fake")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = Settings(_env_file=None)
    assert (s.llm_provider, s.llm_model) == ("fake", "fake-echo-1")
    assert not s.llm_provider_inferred()


def test_settings_nothing_set_is_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    s = Settings(_env_file=None)
    assert (s.llm_provider, s.llm_model) == ("fake", "fake-echo-1")


def test_client_factory_builds_openai_for_inferred_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOOM_LLM_MODEL=gpt-4o-mini + key, provider unset, builds the OpenAI
    client, not FakeLLMClient."""
    from server.llm import client as client_mod
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = Settings(_env_file=None)
    monkeypatch.setattr("server.settings.settings", s)
    client_mod.reset()
    try:
        built = client_mod.get_llm_client()
        assert type(built).__name__ == "OpenAILLMClient"
        assert built.model == "gpt-4o-mini"
    finally:
        client_mod.reset()


def test_boot_log_names_inference_and_missing_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.llm.client import log_llm_provider_resolution
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr("server.settings.settings", Settings(_env_file=None))
    with caplog.at_level(logging.INFO, logger="server.llm.client"):
        log_llm_provider_resolution()
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    msg = infos[0].getMessage()
    assert "inferred llm_provider=openai" in msg
    assert "LOOM_LLM_MODEL=gpt-4o-mini" in msg
    assert "OPENAI_API_KEY is NOT set" in msg


def test_boot_log_is_silent_for_an_explicit_provider(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.llm.client import log_llm_provider_resolution
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_PROVIDER", "fake")
    monkeypatch.setattr("server.settings.settings", Settings(_env_file=None))
    with caplog.at_level(logging.INFO, logger="server.llm.client"):
        log_llm_provider_resolution()
    assert not [r for r in caplog.records if r.levelno == logging.INFO]


def test_doctor_warns_only_when_inferred_provider_lacks_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli.doctor import PASS, WARN, _doctor_llm_provider_check
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr("server.settings.settings", Settings(_env_file=None))
    name, grade, detail = _doctor_llm_provider_check()
    assert (name, grade) == ("llm_provider", WARN)
    assert "OPENAI_API_KEY is not set" in detail and "fake LLM" in detail
    assert "LOOM_LLM_PROVIDER=fake" in detail  # the fix names the env var

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("server.settings.settings", Settings(_env_file=None))
    name, grade, detail = _doctor_llm_provider_check()
    assert grade == PASS
    assert "openai / gpt-4o-mini (inferred from LOOM_LLM_MODEL=gpt-4o-mini)" == detail


def test_doctor_passes_explicit_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli.doctor import PASS, _doctor_llm_provider_check
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_PROVIDER", "fake")
    monkeypatch.setattr("server.settings.settings", Settings(_env_file=None))
    assert _doctor_llm_provider_check() == (
        "llm_provider",
        PASS,
        "fake / fake-echo-1 (set by LOOM_LLM_PROVIDER)",
    )


def test_settings_reject_an_unknown_explicit_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit provider is never overridden, so a typo must fail at load
    (naming the valid ones), not at the first LLM call."""
    from server.core.llm_provider import DEFAULT_MODEL_BY_PROVIDER
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_PROVIDER", "openia")
    with pytest.raises(ValueError, match="LOOM_LLM_PROVIDER='openia'") as err:
        Settings(_env_file=None)
    for known in DEFAULT_MODEL_BY_PROVIDER:
        assert known in str(err.value)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "fake", " OpenAI "])
def test_settings_accept_every_known_explicit_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from server.settings import Settings

    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LOOM_LLM_PROVIDER", provider)
    assert Settings(_env_file=None).llm_provider == provider.strip().lower()
