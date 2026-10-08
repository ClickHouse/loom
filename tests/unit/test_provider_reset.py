"""Environment reset is a versioned transition, not deletion of configuration rows."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from server.insights.api.models_provider_setup import ProviderResetInput, ProviderSetupInput
from server.insights.app import provider_reset as reset


@pytest.fixture
def dependencies(monkeypatch):
    @asynccontextmanager
    async def lock():
        yield None

    setup = ProviderSetupInput.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-test"},
            "embedding": {"provider": "openai", "model": "text-embedding-test"},
            "keys": {"openai": "test-secret"},
        }
    )
    row = {"config_id": uuid4(), "version": 123, "state": "active", "envelope": "encrypted"}
    monkeypatch.setattr(reset, "configuration_lock", lock)
    monkeypatch.setattr(reset, "configurations", AsyncMock(return_value=[row]))
    monkeypatch.setattr(reset, "write_configuration", AsyncMock())
    monkeypatch.setattr(reset, "check_rebuild_inventory", AsyncMock())
    monkeypatch.setattr(reset, "_open_active", Mock(return_value=setup))
    monkeypatch.setattr(
        reset, "environment_embedding", lambda: ("openai", "text-embedding-test", 3072, "", False)
    )
    monkeypatch.setattr(reset, "current_embedding", reset.environment_embedding)
    monkeypatch.setattr(reset, "environment_settings", lambda: {"llm_model": "gpt-environment"})
    monkeypatch.setattr(reset, "apply_environment_configuration", Mock())
    return row


@pytest.mark.asyncio
async def test_same_embedding_restores_environment_without_rebuild(dependencies):
    result = await reset.reset_configuration(ProviderResetInput(expected_version="123"))
    assert result.state == "active"
    row = reset.write_configuration.await_args.args[0]
    assert row["state"] == "environment"
    assert row["parent_id"] == dependencies["config_id"]
    assert row["envelope"] == ""
    assert "test-secret" not in json.dumps(row, default=str)
    reset.apply_environment_configuration.assert_called_once()


@pytest.mark.asyncio
async def test_different_environment_embedding_is_queued(dependencies, monkeypatch):
    monkeypatch.setattr(
        reset, "environment_embedding", lambda: ("gemini", "gemini-test", 3072, "", False)
    )
    result = await reset.reset_configuration(ProviderResetInput(expected_version="123"))
    assert result.state == "queued"
    row = reset.write_configuration.await_args.args[0]
    assert row["state"] == "queued"
    assert json.loads(row["progress"])["action"] == "environment"
    reset.apply_environment_configuration.assert_not_called()
    reset.check_rebuild_inventory.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_reset_cannot_remove_newer_settings(dependencies):
    with pytest.raises(HTTPException) as error:
        await reset.reset_configuration(ProviderResetInput(expected_version="122"))
    assert error.value.status_code == 409
    reset.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "rebuilding", "validating", "failed"])
async def test_reset_cannot_interrupt_an_incomplete_rebuild(dependencies, monkeypatch, state):
    monkeypatch.setattr(
        reset, "configurations", AsyncMock(return_value=[{"state": state}, dependencies])
    )
    with pytest.raises(HTTPException) as error:
        await reset.reset_configuration(ProviderResetInput(expected_version="123"))
    assert error.value.status_code == 409
    reset.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_reset_is_idempotent(dependencies, monkeypatch):
    dependencies["state"] = "environment"
    result = await reset.reset_configuration(ProviderResetInput(expected_version="123"))
    assert result.state == "active"
    reset.write_configuration.assert_not_awaited()
    reset._open_active.assert_not_called()


@pytest.mark.asyncio
async def test_stale_replica_defers_environment_application_to_runtime_drain(
    dependencies, monkeypatch
):
    monkeypatch.setattr(reset, "current_embedding", lambda: ("gemini", "old-model", 768, "", False))
    result = await reset.reset_configuration(ProviderResetInput(expected_version="123"))
    assert result.state == "active"
    reset.apply_environment_configuration.assert_not_called()
