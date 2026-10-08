"""Retry cannot replace a target, skip the cursor or reopen serving prematurely."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from server.insights.api.models_provider_setup import ProviderRetryInput
from server.insights.app import provider_retry as retry


@pytest.fixture
def failed(monkeypatch):
    @asynccontextmanager
    async def lock():
        yield None

    row = {
        "config_id": uuid4(),
        "parent_id": uuid4(),
        "state": "failed",
        "envelope": "encrypted",
        "progress": '{"phase":"rebuild","cursor":123}',
        "error_code": "rebuild_rebuild_failed",
        "created_at": "unchanged",
        "version": 200,
    }
    monkeypatch.setattr(retry, "configuration_lock", lock)
    monkeypatch.setattr(retry, "configurations", AsyncMock(return_value=[row]))
    monkeypatch.setattr(retry, "write_configuration", AsyncMock())
    monkeypatch.setattr(retry, "time_ns", lambda: 100)
    return row


@pytest.mark.asyncio
async def test_retry_preserves_target_checkpoint_and_full_row(failed):
    result = await retry.retry_configuration(ProviderRetryInput(config_id=failed["config_id"]))
    assert result.state == "queued"
    updated = retry.write_configuration.await_args.args[0]
    assert updated == {**failed, "state": "queued", "error_code": "", "version": 201}
    assert failed["state"] == "failed"


@pytest.mark.asyncio
async def test_stale_retry_cannot_resume_a_different_transition(failed):
    with pytest.raises(HTTPException) as error:
        await retry.retry_configuration(ProviderRetryInput(config_id=uuid4()))
    assert error.value.status_code == 409
    retry.write_configuration.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "rebuilding", "validating", "active", "environment"])
async def test_retry_only_accepts_failed_jobs(failed, state):
    failed["state"] = state
    with pytest.raises(HTTPException) as error:
        await retry.retry_configuration(ProviderRetryInput(config_id=failed["config_id"]))
    assert error.value.status_code == 409
    retry.write_configuration.assert_not_awaited()
