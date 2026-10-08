"""A read-replica whose env names no LLM takes the main Loom's published
provider and model; one that names either keeps its own pair."""

from __future__ import annotations

import pytest

from server.core.deployment_config import apply, snapshot
from server.settings import Settings

PUBLISHED = {"llm_provider": "openai", "llm_model": "gpt-6-luna"}


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for var in (
        "LOOM_LLM_PROVIDER",
        "LOOM_LLM_MODEL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LOOM_BOOT_MIGRATE",
        "LOOM_BACKGROUND_WORKERS",
    ):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def _overlay(s: Settings) -> list[str]:
    return apply(s, dict(PUBLISHED), s.env_fields_set())


def test_a_keyless_replica_takes_the_main_looms_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, LOOM_READ_REPLICA="true")
    # Load had nothing to go on and filled in the fake...
    assert (s.llm_provider, s.llm_model) == ("fake", "fake-echo-1")
    assert {"llm_provider", "llm_model"} & s.model_fields_set
    # ...which is not the env's choice, so the published pair replaces it.
    assert not {"llm_provider", "llm_model"} & s.env_fields_set()
    assert set(_overlay(s)) == {"llm_provider", "llm_model"}
    assert (s.llm_provider, s.llm_model) == ("openai", "gpt-6-luna")
    assert s.llm_provider_source() == "the main Loom's published settings"


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ({"LOOM_LLM_MODEL": "claude-haiku-4-5"}, ("anthropic", "claude-haiku-4-5")),
        ({"LOOM_LLM_PROVIDER": "anthropic"}, ("anthropic", None)),
        ({"LOOM_LLM_PROVIDER": "fake", "LOOM_LLM_MODEL": "fake-echo-1"}, ("fake", "fake-echo-1")),
    ],
    ids=["own-model", "own-provider", "own-pair"],
)
def test_a_replica_that_names_either_keeps_its_own_pair(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], want: tuple[str, str | None]
) -> None:
    s = _settings(monkeypatch, LOOM_READ_REPLICA="true", **env)
    before = (s.llm_provider, s.llm_model)
    assert _overlay(s) == []
    assert (s.llm_provider, s.llm_model) == before
    assert s.llm_provider == want[0] and (want[1] is None or s.llm_model == want[1])


def test_the_main_loom_still_publishes_an_inferred_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """A main Loom that only sets a key publishes the provider and model it
    inferred, so its replicas run the same LLM."""
    s = _settings(monkeypatch, OPENAI_API_KEY="sk-test")
    assert s.llm_provider == "openai"
    published = snapshot(s)
    assert published["llm_provider"] == "openai"
    assert published["llm_model"] == s.llm_model
