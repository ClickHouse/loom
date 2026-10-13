"""The conflict judge follows the LLM provider unless the env says otherwise.

It is on wherever a real LLM provider is configured, off on the fake one (its
calls would only fail open there), and LOOM_MEMORY_CONFLICT_JUDGE always wins.
A value load filled in is not the env's choice: a read-replica takes the main
Loom's over it.
"""

from __future__ import annotations

import logging

import pytest

from server.settings import Settings

_ENV = (
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
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("env", "judge"),
    [
        ({"LOOM_LLM_PROVIDER": "fake"}, False),
        ({}, False),
        ({"OPENAI_API_KEY": "sk-test"}, True),
        ({"ANTHROPIC_API_KEY": "sk-ant-test"}, True),
        ({"LOOM_LLM_PROVIDER": "anthropic"}, True),
        ({"LOOM_LLM_MODEL": "gpt-4o-mini"}, True),
        # An explicit provider wins over a key, and the judge follows it.
        ({"LOOM_LLM_PROVIDER": "fake", "OPENAI_API_KEY": "sk-test"}, False),
    ],
)
def test_an_unset_judge_follows_the_llm_provider(monkeypatch, env, judge) -> None:
    s = _load(monkeypatch, **env)
    assert s.memory_conflict_judge is judge
    assert "memory_conflict_judge" not in s.env_fields_set()


def test_the_env_always_wins(monkeypatch) -> None:
    off = _load(monkeypatch, OPENAI_API_KEY="sk-test", LOOM_MEMORY_CONFLICT_JUDGE="false")
    assert off.memory_conflict_judge is False
    assert "memory_conflict_judge" in off.env_fields_set()
    on = _load(monkeypatch, LOOM_LLM_PROVIDER="fake", LOOM_MEMORY_CONFLICT_JUDGE="true")
    assert on.memory_conflict_judge is True
    assert "memory_conflict_judge" in on.env_fields_set()


def test_a_replica_takes_the_main_looms_judge_over_its_own_default(monkeypatch) -> None:
    from server.core import deployment_config

    replica = _load(monkeypatch, LOOM_LLM_PROVIDER="fake")
    changed = deployment_config.apply(
        replica, {"memory_conflict_judge": True}, replica.env_fields_set()
    )
    assert replica.memory_conflict_judge is True and "memory_conflict_judge" in changed


def test_boot_says_what_the_unset_judge_resolved_to(monkeypatch, caplog) -> None:
    from server.llm import client

    s = _load(monkeypatch, OPENAI_API_KEY="sk-test")
    monkeypatch.setattr("server.settings.settings", s)
    with caplog.at_level(logging.INFO, logger=client.__name__):
        client.log_conflict_judge_resolution()
    assert "the conflict judge is on" in caplog.text
    caplog.clear()
    s = _load(monkeypatch, OPENAI_API_KEY="sk-test", LOOM_MEMORY_CONFLICT_JUDGE="false")
    monkeypatch.setattr("server.settings.settings", s)
    with caplog.at_level(logging.INFO, logger=client.__name__):
        client.log_conflict_judge_resolution()
    assert not caplog.records
