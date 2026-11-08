"""The onboarding contract: hosted embedders and the local one, never the fake one."""

import pytest
from pydantic import ValidationError

from server.insights.api.models_provider_setup import EmbeddingSetup, LLMSetup, ProviderSetupInput
from server.insights.domain.provider_setup import llm_setup_settings


@pytest.mark.parametrize("provider", ["fake", "anthropic", "", "voyage"])
def test_embedding_setup_rejects_unsupported_providers(provider: str) -> None:
    with pytest.raises(ValidationError):
        EmbeddingSetup.model_validate({"provider": provider, "model": "test-model"})


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_embedding_setup_accepts_hosted_providers(provider: str) -> None:
    setup = EmbeddingSetup.model_validate({"provider": provider, "model": " test-model "})
    assert setup.model == "test-model"
    assert setup.dimensions == 3072


@pytest.mark.parametrize(
    ("model", "dimensions"), [("local", 384), ("local:bge-base", 768), ("local:minilm", 384)]
)
def test_a_local_model_is_set_up_at_its_own_width(model: str, dimensions: int) -> None:
    setup = EmbeddingSetup(provider="local", model=model, dimensions=dimensions)
    assert (setup.model, setup.dimensions) == (model, dimensions)


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "local:gigantic"},
        {"model": "text-embedding-3-large"},
        {"dimensions": 768},
        {"base_url": "https://example.com/v1"},
    ],
)
def test_a_local_setup_refuses_another_model_width_or_endpoint(changes: dict) -> None:
    with pytest.raises(ValidationError):
        EmbeddingSetup.model_validate(
            {"provider": "local", "model": "local", "dimensions": 384, **changes}
        )


def test_compatible_provider_requires_explicit_endpoint() -> None:
    with pytest.raises(ValidationError):
        EmbeddingSetup(provider="compatible", model="custom")
    setup = EmbeddingSetup(
        provider="compatible",
        model="custom",
        dimensions=768,
        base_url=" https://embedding.example/v1/ ",
    )
    assert setup.base_url == "https://embedding.example/v1"
    assert setup.send_dimensions is False
    wide = setup.model_copy(update={"dimensions": 4096})
    assert EmbeddingSetup.model_validate(wide.model_dump()).dimensions == 4096


@pytest.mark.parametrize(
    "base_url", ["https://key@example.com", "https://example.com?key=x", "file:///tmp/model"]
)
def test_compatible_endpoint_rejects_unsafe_url_shapes(base_url: str) -> None:
    with pytest.raises(ValidationError):
        EmbeddingSetup(provider="compatible", model="custom", base_url=base_url)


@pytest.mark.parametrize("dimensions", [0, -1, 3073, True, 3.5, "3072"])
def test_embedding_dimensions_are_validated(dimensions: object) -> None:
    with pytest.raises(ValidationError):
        EmbeddingSetup.model_validate(
            {"provider": "openai", "model": "test", "dimensions": dimensions}
        )


def test_provider_keys_are_excluded_from_serialization() -> None:
    setup = ProviderSetupInput.model_validate(
        {
            "llm": {"provider": "anthropic", "model": "claude-test"},
            "embedding": {"provider": "openai", "model": "text-embedding-test"},
            "keys": {"anthropic": "test-secret-value"},
        }
    )
    assert "keys" not in setup.model_dump()
    assert "test-secret-value" not in repr(setup)
    assert "test-secret-value" not in setup.model_dump_json()
    assert setup.keys["anthropic"].get_secret_value() == "test-secret-value"


def test_unknown_job_cannot_change_arbitrary_server_settings() -> None:
    with pytest.raises(ValidationError):
        LLMSetup.model_validate(
            {"provider": "openai", "model": "test", "overrides": {"auth_required": "false"}}
        )


def test_one_model_defaults_keep_verification_enabled() -> None:
    settings = llm_setup_settings("anthropic", "claude-test", {}, None)
    assert settings["llm_model"] == "claude-test"
    assert settings["memory_extract_verify_models"] == "claude-test"
    assert settings["session_brief_llm_model"] == ""
    assert settings["tracker_dupcheck_rerank_model"] == ""
    assert settings["retrieval_judge_model"] == ""
    assert settings["memory_ask_synth_model_cheap"] == ""


def test_advanced_jobs_and_explicit_verifier_disable() -> None:
    settings = llm_setup_settings("openai", "gpt-test", {"code_ask_llm_model": "gpt-other"}, [])
    assert settings["code_ask_llm_model"] == "gpt-other"
    assert settings["memory_extract_verify_models"] == ""
