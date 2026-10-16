"""Coordinator ownership, immutable recipes and bounded durable steps."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from server.core.embedding_space import fingerprint as embedding_space_fingerprint
from server.insights.app import provider_coordinator as coordinator
from server.settings import settings


@pytest.fixture
def setup(monkeypatch):
    row = {"config_id": uuid4(), "state": "queued", "version": 1, "progress": "{}"}
    identity = ("compatible", "same-model", 8, "http://embed/v1", False)
    monkeypatch.setattr(coordinator, "configurations", AsyncMock(return_value=[row]))
    monkeypatch.setattr(coordinator, "acknowledge_drain", AsyncMock(return_value=True))
    monkeypatch.setattr(coordinator, "distributed_drain", AsyncMock(return_value=True))
    monkeypatch.setattr(coordinator, "sweep_stopped", AsyncMock())
    monkeypatch.setattr(coordinator, "_target", lambda _: (None, identity))
    monkeypatch.setattr(coordinator, "rebuild_target", Mock())
    monkeypatch.setattr(coordinator, "capture_rebuild_views", AsyncMock(return_value=()))
    writes = AsyncMock()
    step = AsyncMock()
    monkeypatch.setattr(coordinator, "write_configuration", writes)
    monkeypatch.setattr(coordinator, "run_rebuild_step", step)
    owned = []
    held = SimpleNamespace(held=True)

    @asynccontextmanager
    async def lock():
        owned.append(True)
        try:
            yield held
        finally:
            owned.pop()

    monkeypatch.setattr(coordinator, "configuration_lock", lock)
    monkeypatch.setattr(settings, "code_vector_tables", "{}")
    monkeypatch.setattr(coordinator, "_is_leader", lambda: True)
    return row, identity, writes, step, owned


@pytest.mark.asyncio
async def test_freezes_recipe_before_step_under_exclusive_owner(setup):
    row, identity, writes, step, owned = setup

    async def run(current, backend, write):
        assert owned
        writes.assert_awaited_once()
        recipe = json.loads(current["progress"])["recipe"]
        assert recipe["fingerprint"] == embedding_space_fingerprint(*identity)
        assert recipe["prefix_dimensions"] == []
        assert coordinator.unseal("provider-rebuild-views", recipe["views"]) == "[]"
        assert backend.fingerprint == recipe["fingerprint"]

    step.side_effect = run
    await coordinator.coordinate_once()
    step.assert_awaited_once()
    assert not owned
    assert row["progress"] == "{}"


@pytest.mark.asyncio
async def test_not_every_participant_drained_means_no_effects(setup, monkeypatch):
    _, _, writes, step, _ = setup
    monkeypatch.setattr(coordinator, "distributed_drain", AsyncMock(return_value=False))
    await coordinator.coordinate_once()
    writes.assert_not_awaited()
    step.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_job_stays_closed_until_operator_retry(setup):
    row, _, writes, step, _ = setup
    row["state"] = "failed"
    await coordinator.coordinate_once()
    coordinator.acknowledge_drain.assert_awaited_once()
    writes.assert_not_awaited()
    step.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_keeps_original_prefix_recipe(setup, monkeypatch):
    row, identity, writes, _, _ = setup
    monkeypatch.setattr(
        settings,
        "code_vector_tables",
        json.dumps(
            {
                "prefix:4:old": {"table": "loom.code_chunks_prefix", "dim": 4},
                "prefix:9:old": {"table": "loom.code_chunks_prefix", "dim": 9},
                "foreign": {"table": "loom.custom", "dim": 2},
            }
        ),
    )
    frozen = await coordinator._freeze_recipe(row, identity, writes)
    assert json.loads(frozen["progress"])["recipe"]["prefix_dimensions"] == [4]
    monkeypatch.setattr(settings, "code_vector_tables", "{}")
    assert await coordinator._freeze_recipe(frozen, identity, writes) == frozen
    writes.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_rejects_changed_target(setup):
    row, identity, writes, _, _ = setup
    frozen = await coordinator._freeze_recipe(row, identity, writes)
    with pytest.raises(ValueError, match="Frozen rebuild"):
        await coordinator._freeze_recipe(frozen, (*identity[:3], "http://other/v1", False), writes)


@pytest.mark.asyncio
async def test_unsupported_view_recipe_becomes_retryable_without_exposing_ddl(setup, monkeypatch):
    row, _, writes, step, _ = setup
    monkeypatch.setattr(
        coordinator,
        "capture_rebuild_views",
        AsyncMock(side_effect=ValueError("operator DDL containing private credentials")),
    )
    await coordinator.coordinate_once()
    step.assert_not_awaited()
    written = writes.call_args.args[0]
    assert written["state"] == "failed" and written["error_code"] == "rebuild_recipe_failed"
    assert written["progress"] == row["progress"]
    assert "private credentials" not in str(written)


@pytest.mark.asyncio
async def test_a_lost_lock_commits_nothing(setup, monkeypatch):
    """The lock's renewal can fail while the step runs: the writes that
    follow are refused, not raced against the next holder."""
    _, _, writes, step, _ = setup

    @asynccontextmanager
    async def lost():
        yield SimpleNamespace(held=False)

    monkeypatch.setattr(coordinator, "configuration_lock", lost)
    await coordinator.coordinate_once()
    writes.assert_not_awaited()
    step.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_the_workers_leader_sweeps_participants(setup, monkeypatch):
    monkeypatch.setattr(coordinator, "_is_leader", lambda: False)
    await coordinator.coordinate_once()
    coordinator.sweep_stopped.assert_not_awaited()
    monkeypatch.setattr(coordinator, "_is_leader", lambda: True)
    await coordinator.coordinate_once()
    coordinator.sweep_stopped.assert_awaited_once()
