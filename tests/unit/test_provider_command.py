"""Native commands remain participants until their deferred writes finish."""

from unittest.mock import AsyncMock

import pytest

from server.core.model_work_gate import ModelChangeInProgress, ModelWorkGate
from server.insights.app import provider_command as command


@pytest.fixture
def setup(monkeypatch):
    gate = ModelWorkGate()
    monkeypatch.setattr(command, "get_model_work_gate", lambda: gate)
    monkeypatch.setattr(command, "register_process", AsyncMock())
    monkeypatch.setattr(command, "retire_process", AsyncMock())
    monkeypatch.setattr(command, "load_active_configuration", AsyncMock())
    monkeypatch.setattr(command, "drain_task_sets", AsyncMock(return_value=True))
    monkeypatch.setattr(command, "flush_registered_buffers", AsyncMock(return_value=True))
    return gate


@pytest.mark.asyncio
async def test_command_enrolls_loads_settings_then_admits_and_retires(setup):
    async with command.model_command():
        assert setup.active == 1
        command.register_process.assert_awaited_once()
        command.load_active_configuration.assert_awaited_once()
        command.retire_process.assert_not_awaited()
    assert setup.active == 0
    command.retire_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_transition_never_runs_command(setup):
    setup.close("transition")
    with pytest.raises(ModelChangeInProgress):
        async with command.model_command():
            pytest.fail("closed admission must reject a newborn command")
    command.retire_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_undrained_descendants_keep_participant_registered(setup, monkeypatch):
    monkeypatch.setattr(command, "drain_task_sets", AsyncMock(return_value=False))
    async with command.model_command():
        pass
    command.retire_process.assert_not_awaited()
    command.flush_registered_buffers.assert_not_awaited()


@pytest.mark.asyncio
async def test_business_failure_does_not_skip_drain(setup):
    with pytest.raises(ValueError, match="business failure"):
        async with command.model_command():
            raise ValueError("business failure")
    command.retire_process.assert_awaited_once()
