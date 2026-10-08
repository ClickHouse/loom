"""Secrets remain ciphertext and state transitions cannot erase columns."""

from unittest.mock import AsyncMock

import pytest

from server.core import secrets_at_rest
from server.insights.api.models_provider_setup import ProviderSetupInput
from server.insights.app.provider_credentials import open_configuration, seal_configuration
from server.insights.store.provider_configuration import write_configuration
from tests.unit.schema_ddl import column_names


def test_configuration_columns_match_schema() -> None:
    from server.insights.store.provider_configuration import _COLUMNS

    assert set(_COLUMNS) == set(column_names("provider_configuration", materialized=False))


@pytest.fixture
def setup() -> ProviderSetupInput:
    return ProviderSetupInput.model_validate(
        {
            "llm": {"provider": "anthropic", "model": "claude-test"},
            "embedding": {"provider": "openai", "model": "text-embedding-test"},
            "keys": {"anthropic": "new-secret"},
        }
    )


def test_encrypted_envelope_round_trip(
    monkeypatch: pytest.MonkeyPatch, setup: ProviderSetupInput
) -> None:
    monkeypatch.setattr(
        secrets_at_rest.settings, "jwt_secret", "test-signing-secret-at-least-32-chars"
    )
    envelope = seal_configuration(setup, {"anthropic": "old-secret", "openai": "embedding-secret"})
    assert "new-secret" not in envelope
    assert "embedding-secret" not in envelope
    restored = open_configuration(envelope)
    assert restored.llm == setup.llm
    assert restored.embedding == setup.embedding
    assert restored.keys["anthropic"].get_secret_value() == "new-secret"
    assert restored.keys["openai"].get_secret_value() == "embedding-secret"


def test_rotated_secret_requires_reentry(
    monkeypatch: pytest.MonkeyPatch, setup: ProviderSetupInput
) -> None:
    monkeypatch.setattr(
        secrets_at_rest.settings, "jwt_secret", "first-signing-secret-at-least-32-chars"
    )
    envelope = seal_configuration(setup, {})
    monkeypatch.setattr(
        secrets_at_rest.settings, "jwt_secret", "second-signing-secret-at-least-32-chars"
    )
    with pytest.raises(ValueError, match="provider_configuration_unreadable"):
        open_configuration(envelope)


def test_invalid_payload_never_echoes_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        secrets_at_rest.settings, "jwt_secret", "test-signing-secret-at-least-32-chars"
    )
    envelope = secrets_at_rest.seal(
        "model-provider-configuration", '{"format":1,"setup":"secret-value"}'
    )
    with pytest.raises(ValueError) as exc:
        open_configuration(envelope)
    assert "secret-value" not in str(exc.value)


def test_keys_cannot_be_saved_under_a_weak_secret(
    monkeypatch: pytest.MonkeyPatch, setup: ProviderSetupInput
) -> None:
    monkeypatch.setattr(secrets_at_rest.settings, "jwt_secret", "short")
    with pytest.raises(ValueError, match="unique LOOM_JWT_SECRET"):
        seal_configuration(setup, {})


@pytest.mark.asyncio
async def test_partial_rmt_rows_are_rejected() -> None:
    client = AsyncMock()
    with pytest.raises(ValueError, match="every persisted column"):
        await write_configuration({"config_id": "id", "state": "active"}, ch=client)
    client.insert.assert_not_called()


@pytest.mark.asyncio
async def test_full_row_is_preserved() -> None:
    client = AsyncMock()
    row = dict.fromkeys(
        (
            "config_id",
            "parent_id",
            "state",
            "envelope",
            "progress",
            "error_code",
            "created_at",
            "version",
        )
    )
    await write_configuration(row, ch=client)
    client.insert.assert_awaited_once_with("loom.provider_configuration", [row])
