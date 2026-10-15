"""The conflict judge follows the LLM provider unless the env says otherwise.

Unset, it is on while the resolved LLM provider is a real one whose key is present
(from the env or saved from the dashboard), and off otherwise; it is decided on each
use, so a key added at runtime turns it on. LOOM_MEMORY_CONFLICT_JUDGE always wins.
"""

from __future__ import annotations

import logging

import pytest

from server.core import provider_keys
from server.settings import Settings

_ENV = (
    "LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP",
    "LOOM_READ_REPLICA",
    "LOOM_READ_REPLICA_MAIN_URL",
    "LOOM_LLM_PROVIDER",
    "LOOM_LLM_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LOOM_MEMORY_CONFLICT_JUDGE",
)


def _load(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr("server.settings.settings", s)
    return s


@pytest.fixture(autouse=True)
def _no_saved_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_keys, "_keys", {})


def _on() -> bool:
    from server.core.conflict_judge_switch import conflict_judge_on

    return conflict_judge_on()


@pytest.mark.parametrize(
    ("env", "judge"),
    [
        ({"LOOM_LLM_PROVIDER": "fake"}, False),
        ({}, False),
        ({"OPENAI_API_KEY": "sk-test"}, True),
        ({"ANTHROPIC_API_KEY": "sk-ant-test"}, True),
        ({"LOOM_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test"}, True),
        ({"LOOM_LLM_MODEL": "gpt-4o-mini", "OPENAI_API_KEY": "sk-test"}, True),
        # A real provider with no key runs on the fake client: the judge stays off.
        ({"LOOM_LLM_PROVIDER": "openai"}, False),
        ({"LOOM_LLM_MODEL": "gpt-4o-mini"}, False),
        # An explicit provider wins over a key, and the judge follows it.
        ({"LOOM_LLM_PROVIDER": "fake", "OPENAI_API_KEY": "sk-test"}, False),
        # Empty or "auto" is unset.
        ({"OPENAI_API_KEY": "sk-test", "LOOM_MEMORY_CONFLICT_JUDGE": ""}, True),
        ({"OPENAI_API_KEY": "sk-test", "LOOM_MEMORY_CONFLICT_JUDGE": "auto"}, True),
    ],
)
def test_an_unset_judge_follows_the_llm_provider_and_its_key(monkeypatch, env, judge) -> None:
    s = _load(monkeypatch, **env)
    assert s.memory_conflict_judge is None
    assert _on() is judge


def test_the_env_always_wins(monkeypatch) -> None:
    _load(monkeypatch, OPENAI_API_KEY="sk-test", LOOM_MEMORY_CONFLICT_JUDGE="false")
    assert _on() is False
    _load(monkeypatch, LOOM_LLM_PROVIDER="fake", LOOM_MEMORY_CONFLICT_JUDGE="true")
    assert _on() is True


def test_a_key_saved_from_the_dashboard_turns_it_on(monkeypatch) -> None:
    # The stock stack boots on the fake provider; the dashboard then sets the
    # provider and saves its key at runtime.
    s = _load(monkeypatch, LOOM_LLM_PROVIDER="fake")
    assert _on() is False
    monkeypatch.setattr(s, "llm_provider", "openai")
    assert _on() is False  # no key yet
    provider_keys.install_provider_keys({"openai": "sk-saved"})
    assert _on() is True


def test_an_unset_judge_is_not_published_to_a_replica(monkeypatch) -> None:
    s = _load(monkeypatch, OPENAI_API_KEY="sk-test")
    assert "memory_conflict_judge" not in s.env_fields_set()
    assert "memory_conflict_judge" not in s.model_fields_set


def test_boot_says_what_the_unset_judge_is_and_why(monkeypatch, caplog) -> None:
    from server.core import conflict_judge_switch as switch

    _load(monkeypatch, LOOM_LLM_PROVIDER="openai")
    with caplog.at_level(logging.INFO, logger=switch.__name__):
        switch.log_conflict_judge_resolution()
    assert "the conflict judge is off" in caplog.text
    assert "OPENAI_API_KEY is not set" in caplog.text
    caplog.clear()
    _load(monkeypatch, OPENAI_API_KEY="sk-test")
    with caplog.at_level(logging.INFO, logger=switch.__name__):
        switch.log_conflict_judge_resolution()
    assert "the conflict judge is on" in caplog.text
    caplog.clear()
    _load(monkeypatch, OPENAI_API_KEY="sk-test", LOOM_MEMORY_CONFLICT_JUDGE="false")
    with caplog.at_level(logging.INFO, logger=switch.__name__):
        switch.log_conflict_judge_resolution()
    assert not caplog.records


def test_a_replica_that_completes_through_the_main_loom_runs_the_judge(monkeypatch) -> None:
    s = _load(monkeypatch, LOOM_LLM_PROVIDER="openai")
    assert _on() is False
    monkeypatch.setattr(s, "read_replica", True)
    monkeypatch.setattr(s, "read_replica_main_url", "http://main:7777")
    assert _on() is True


def test_the_judges_own_model_decides_which_key_it_needs(monkeypatch) -> None:
    # The judge calls the cheap synth model when one is set: an Anthropic model
    # with only an OpenAI key would run on the fake client.
    _load(
        monkeypatch,
        OPENAI_API_KEY="sk-test",
        LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP="claude-haiku-4-5",
    )
    assert _on() is False
    _load(
        monkeypatch,
        OPENAI_API_KEY="sk-test",
        ANTHROPIC_API_KEY="sk-ant-test",
        LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP="claude-haiku-4-5",
    )
    assert _on() is True


def test_an_empty_value_is_not_the_envs_choice(monkeypatch) -> None:
    # A replica with `LOOM_MEMORY_CONFLICT_JUDGE=` still takes the main Loom's value.
    from server.core.deployment_config import explicit_fields

    s = _load(monkeypatch, LOOM_MEMORY_CONFLICT_JUDGE="")
    assert "memory_conflict_judge" not in explicit_fields(s)
    s = _load(monkeypatch, LOOM_MEMORY_CONFLICT_JUDGE="false")
    assert "memory_conflict_judge" in explicit_fields(s)


def test_doctor_says_whether_the_judge_runs_now(monkeypatch) -> None:
    from server.cli.doctor import _doctor_conflict_judge_check

    _load(monkeypatch, OPENAI_API_KEY="sk-test")
    name, _status, detail = _doctor_conflict_judge_check()
    assert name == "conflict_judge" and detail.startswith("on (")
    _load(monkeypatch, LOOM_LLM_PROVIDER="fake")
    assert _doctor_conflict_judge_check()[2].startswith("off (")
