"""Changing keys or LLMs does not silently switch the embedding space."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from server.insights.api.models_provider_setup import ProviderSetupInput, ProviderValidation
from server.insights.app import provider_activation as activation


def setup_input(**changes: object) -> ProviderSetupInput:
    body: dict[str, object] = {
        "llm": {"provider": "anthropic", "model": "claude-test"},
        "embedding": {"provider": "openai", "model": "text-embedding-test", "dimensions": 3072},
        "keys": {"anthropic": "new-key"},
        "expected_version": "0",
    }
    body.update(changes)
    return ProviderSetupInput.model_validate(body)


@pytest.fixture
def dependencies(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def lock():
        yield None

    monkeypatch.setattr(activation, "configuration_lock", lock)
    monkeypatch.setattr(activation, "configurations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        activation,
        "validate_providers",
        AsyncMock(return_value=ProviderValidation(ready=True, checks=[])),
    )
    monkeypatch.setattr(activation, "seal_configuration", Mock(return_value="ciphertext"))
    monkeypatch.setattr(activation, "open_configuration", Mock(return_value=setup_input()))
    monkeypatch.setattr(activation, "write_configuration", AsyncMock())
    monkeypatch.setattr(activation, "rebuild_inventory", AsyncMock(return_value=()))
    monkeypatch.setattr(activation, "unpublishable_rebuild_tables", AsyncMock(return_value=()))
    monkeypatch.setattr(activation, "tables_with_vectors", AsyncMock(return_value=("chunks",)))
    monkeypatch.setattr(activation, "apply_configuration", Mock())
    monkeypatch.setattr(activation, "provider_for", lambda _: "openai")
    monkeypatch.setattr(activation.settings, "embedding_model", "text-embedding-test")
    monkeypatch.setattr(activation.settings, "embedding_dim", 3072)


@pytest.mark.asyncio
async def test_key_and_llm_update_can_activate_without_reembedding(dependencies) -> None:
    result = await activation.save_configuration(setup_input())
    assert result.state == "active"
    activation.apply_configuration.assert_called_once()
    row = activation.write_configuration.await_args.args[0]
    assert row["state"] == "active" and row["envelope"] == "ciphertext"


@pytest.mark.asyncio
async def test_validation_uses_persisted_credentials_before_replica_refresh(
    dependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(activation, "environment_provider_key", lambda _: "environment-key")
    monkeypatch.setattr(
        activation,
        "configurations",
        AsyncMock(
            return_value=[
                {"config_id": uuid4(), "state": "active", "version": 123, "envelope": "old"}
            ]
        ),
    )
    await activation.save_configuration(setup_input(expected_version="123", keys={}))
    checked_keys = activation.validate_providers.await_args.args[1]
    assert checked_keys["anthropic"] == "new-key"
    assert checked_keys["openai"] == "environment-key"


@pytest.mark.asyncio
async def test_revision_changed_during_validation_cannot_be_saved(
    dependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        activation,
        "configurations",
        AsyncMock(
            side_effect=[
                [],
                [{"config_id": uuid4(), "state": "active", "version": 123, "envelope": "old"}],
            ]
        ),
    )
    with pytest.raises(HTTPException) as error:
        await activation.save_configuration(setup_input())
    assert error.value.status_code == 409
    activation.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedding_change_is_queued_not_applied(dependencies) -> None:
    result = await activation.save_configuration(
        setup_input(embedding={"provider": "gemini", "model": "gemini-embedding-001"})
    )
    assert result.state == "queued"
    activation.apply_configuration.assert_not_called()
    activation.rebuild_inventory.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_vector_surface_prevents_accepting_partial_rebuild(dependencies, monkeypatch):
    monkeypatch.setattr(
        activation, "rebuild_inventory", AsyncMock(side_effect=ValueError("unknown custom table"))
    )
    with pytest.raises(HTTPException) as error:
        await activation.save_configuration(
            setup_input(embedding={"provider": "gemini", "model": "gemini-embedding-001"})
        )
    assert error.value.status_code == 409
    activation.write_configuration.assert_not_awaited()
    activation.apply_configuration.assert_not_called()


@pytest.mark.asyncio
async def test_embedding_change_on_unpublishable_storage_is_refused(dependencies, monkeypatch):
    monkeypatch.setattr(
        activation,
        "unpublishable_rebuild_tables",
        AsyncMock(
            return_value=(
                ("code_chunks", "SharedReplacingMergeTree"),
                ("memory_embeddings", "SharedReplacingMergeTree"),
            )
        ),
    )
    with pytest.raises(HTTPException) as error:
        await activation.save_configuration(
            setup_input(embedding={"provider": "gemini", "model": "gemini-embedding-001"})
        )
    assert error.value.status_code == 409
    assert "2 tables, such as loom.code_chunks (SharedReplacingMergeTree)" in str(
        error.value.detail
    )
    activation.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_embedding_choice_on_unpublishable_storage_switches_directly(
    dependencies, monkeypatch
):
    blocked = (("memory_embeddings", "SharedReplacingMergeTree"),)
    monkeypatch.setattr(activation, "unpublishable_rebuild_tables", AsyncMock(return_value=blocked))
    monkeypatch.setattr(activation, "tables_with_vectors", AsyncMock(return_value=()))
    result = await activation.save_configuration(
        setup_input(embedding={"provider": "gemini", "model": "gemini-embedding-001"})
    )
    assert result.state == "active"
    assert activation.write_configuration.await_args.args[0]["state"] == "active"
    # The refresh loop drains each process before it switches embedding spaces.
    activation.apply_configuration.assert_not_called()


@pytest.mark.asyncio
async def test_first_embedding_choice_on_local_storage_skips_the_rebuild(dependencies, monkeypatch):
    monkeypatch.setattr(activation, "tables_with_vectors", AsyncMock(return_value=()))
    result = await activation.save_configuration(
        setup_input(embedding={"provider": "gemini", "model": "gemini-embedding-001"})
    )
    assert result.state == "active"
    activation.unpublishable_rebuild_tables.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_change_on_unpublishable_storage_still_activates(dependencies, monkeypatch):
    blocked = AsyncMock(return_value=(("memory_embeddings", "SharedReplacingMergeTree"),))
    monkeypatch.setattr(activation, "unpublishable_rebuild_tables", blocked)
    result = await activation.save_configuration(setup_input())
    assert result.state == "active"
    blocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_writer_cannot_replace_keys(
    dependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        activation,
        "configurations",
        AsyncMock(
            return_value=[
                {"config_id": uuid4(), "state": "active", "version": 123, "envelope": "old"}
            ]
        ),
    )
    with pytest.raises(HTTPException) as error:
        await activation.save_configuration(setup_input())
    assert error.value.status_code == 409
    activation.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_validation_cannot_persist(
    dependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        activation,
        "validate_providers",
        AsyncMock(return_value=ProviderValidation(ready=False, checks=[])),
    )
    with pytest.raises(HTTPException) as error:
        await activation.save_configuration(setup_input())
    assert error.value.status_code == 422
    activation.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "rebuilding", "validating", "failed"])
async def test_only_one_pending_configuration(
    dependencies, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setattr(activation, "configurations", AsyncMock(return_value=[{"state": state}]))
    with pytest.raises(HTTPException) as error:
        await activation.save_configuration(setup_input())
    assert error.value.status_code == 409
    activation.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedding_comparison_uses_committed_revision_not_stale_replica(
    dependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        activation,
        "configurations",
        AsyncMock(
            return_value=[
                {"config_id": uuid4(), "state": "active", "version": 123, "envelope": "old"}
            ]
        ),
    )
    # This replica has not refreshed the committed OpenAI configuration yet.
    monkeypatch.setattr(activation.settings, "embedding_model", "stale-model")
    monkeypatch.setattr(activation, "time_ns", lambda: 100)
    result = await activation.save_configuration(setup_input(expected_version="123"))
    assert result.state == "active"
    activation.apply_configuration.assert_not_called()
    assert activation.write_configuration.await_args.args[0]["version"] == 124


@pytest.mark.asyncio
async def test_endpoint_change_requires_rebuild_even_for_same_model(
    dependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = setup_input(
        embedding={
            "provider": "compatible",
            "model": "shared-name",
            "dimensions": 3072,
            "base_url": "https://old.example/v1",
        }
    )
    monkeypatch.setattr(
        activation,
        "configurations",
        AsyncMock(
            return_value=[
                {"config_id": uuid4(), "state": "active", "version": 123, "envelope": "old"}
            ]
        ),
    )
    monkeypatch.setattr(activation, "open_configuration", Mock(return_value=previous))
    result = await activation.save_configuration(
        setup_input(
            expected_version="123",
            embedding={**previous.embedding.model_dump(), "base_url": "https://new.example/v1"},
        )
    )
    assert result.state == "queued"
    activation.apply_configuration.assert_not_called()
